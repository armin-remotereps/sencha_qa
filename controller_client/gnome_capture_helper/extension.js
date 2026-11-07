// Test Runner capture helper (GNOME Shell 45-49).
//
// GNOME restricts window lists and screenshots to its own callers, so the
// Test Runner on this computer asks this small extension instead, over the
// session bus name org.testrunner.Capture1. It can only:
//   - list the windows, workspaces and monitors (ListWindows),
//   - save a screen area or one window's own picture as a PNG file
//     (CaptureArea, CaptureWindow), and only into the Test Runner's private
//     folder $XDG_RUNTIME_DIR/test-runner-capture (mode 0700, owned by this
//     user); any other path is refused,
//   - read and change the shown workspace (ActiveWorkspace,
//     ActivateWorkspace).
// No input, no eval, no network. Every call catches its own errors and
// answers with a D-Bus error, so nothing reaches the Shell.
//
// Caller check: every method but Version takes the Test Runner's token as
// its first argument. The runner writes it into helper-token (mode 0600) in
// its private folder; a call whose token does not match that file, or when
// the file is not a regular 0600 file of this user, is refused and logged
// with the caller's process id, so other programs of the same user cannot
// list windows, capture or switch workspaces through this extension.
//
// Bump HELPER_VERSION when the interface changes; the runner refuses a helper
// older than the version it needs (controller_client/gnome_shell_capture.py).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Mtk from 'gi://Mtk';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');
Gio._promisify(Shell.Screenshot, 'composite_to_stream');

const HELPER_VERSION = 2;
const BUS_NAME = 'org.testrunner.Capture1';
const OBJECT_PATH = '/org/testrunner/Capture1';
const RUNNER_DIR_NAME = 'test-runner-capture';
const TOKEN_FILE_NAME = 'helper-token';
const TOKEN_RE = /^[0-9a-f]{64}$/;
const TOKEN_MAX_BYTES = 256;
const FILE_NAME_RE = /^[A-Za-z0-9_-]{1,64}\.png$/;
const MAX_AREA_SIDE = 32768;
const ERROR_REFUSED = 'org.testrunner.Capture1.Error.Refused';
const ERROR_NOT_FOUND = 'org.testrunner.Capture1.Error.NotFound';
const ERROR_FAILED = 'org.testrunner.Capture1.Error.Failed';
const ERROR_UNAUTHORIZED = 'org.testrunner.Capture1.Error.Unauthorized';

const INTERFACE_XML = `
<node>
  <interface name="org.testrunner.Capture1">
    <method name="Version">
      <arg type="u" direction="out" name="version"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="in" name="token"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="CaptureArea">
      <arg type="s" direction="in" name="token"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="path"/>
    </method>
    <method name="CaptureWindow">
      <arg type="s" direction="in" name="token"/>
      <arg type="s" direction="in" name="id"/>
      <arg type="s" direction="in" name="path"/>
    </method>
    <method name="ActiveWorkspace">
      <arg type="s" direction="in" name="token"/>
      <arg type="i" direction="out" name="index"/>
    </method>
    <method name="ActivateWorkspace">
      <arg type="s" direction="in" name="token"/>
      <arg type="i" direction="in" name="index"/>
    </method>
  </interface>
</node>`;

class HelperError extends Error {
    constructor(dbusName, message) {
        super(message);
        this.dbusName = dbusName;
    }
}

function answerError(invocation, error) {
    const name = error instanceof HelperError ? error.dbusName : ERROR_FAILED;
    const message = error instanceof HelperError ? error.message : 'internal error';
    if (!(error instanceof HelperError))
        console.error(`Test Runner capture: ${error}`);
    invocation.return_dbus_error(name, message);
}

const APP_TYPES = [
    Meta.WindowType.NORMAL,
    Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG,
];
// Panels, docks and the desktop. Any client can declare its window one of
// these, so such a window is left out only when it is the Shell's own or its
// process is a known shell, panel or desktop (isShellSurface); every other
// one is listed as an overlay.
const SHELL_TYPES = [
    Meta.WindowType.DOCK,
    Meta.WindowType.DESKTOP,
];
// Copy of controller_client/shell_processes.py LINUX_SHELL_SURFACE_PROCESSES
// (names as in /proc/<pid>/comm, at most 15 characters): change both
// together.
const SHELL_SURFACE_PROCESSES = new Set([
    'plasmashell',
    'xfce4-panel',
    'xfdesktop',
    'gnome-shell',
    'mate-panel',
    'caja',
    'nemo-desktop',
    'cinnamon',
    'budgie-panel',
    'lxpanel',
    'lxqt-panel',
    'pcmanfm',
    'pcmanfm-qt',
    'tint2',
    'polybar',
    'xfce4-notifyd',
]);
const SHELL_PID = new Gio.Credentials().get_unix_pid();

// The process name of ``pid`` (/proc/<pid>/comm, lower case), or '' when it
// cannot be read.
function processName(pid) {
    if (!Number.isInteger(pid) || pid <= 0)
        return '';
    try {
        const [ok, bytes] = GLib.file_get_contents(`/proc/${pid}/comm`);
        return ok ? new TextDecoder().decode(bytes).trim().toLowerCase() : '';
    } catch {
        return '';
    }
}

// Whether a dock or desktop window is the Shell's own or a known shell
// process's (left out). An unknown process is not (listed as an overlay).
function isShellSurface(window, names) {
    const pid = window.get_pid();
    if (!Number.isInteger(pid) || pid <= 0)
        return false;
    if (pid === SHELL_PID)
        return true;
    if (!names.has(pid))
        names.set(pid, processName(pid));
    return SHELL_SURFACE_PROCESSES.has(names.get(pid));
}

function rectObject(rect) {
    return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
}

function describeWindow(window, actor, app) {
    const workspace = window.get_workspace();
    const onAll = window.is_on_all_workspaces() || (!app && !workspace);
    return {
        id: String(window.get_id()),
        kind: app ? 'app' : 'overlay',
        pid: window.get_pid(),
        title: app ? window.get_title() ?? '' : '',
        frame: rectObject(window.get_frame_rect()),
        minimized: Boolean(window.minimized),
        focused: window.has_focus(),
        drawn: actor.visible,
        on_all_workspaces: onAll,
        workspace: onAll || !workspace ? -1 : workspace.index(),
    };
}

// Every window, bottom to top in stacking order: app windows ("app") and
// every other visible window ("overlay": menus, tooltips, utility windows,
// notifications of other programs, docks of unknown processes), without the
// shell's own panels and desktop.
function listedWindows() {
    const windows = [];
    const names = new Map();
    for (const actor of global.get_window_actors()) {
        const window = actor.get_meta_window();
        if (!window)
            continue;
        const type = window.get_window_type();
        if (SHELL_TYPES.includes(type) && isShellSurface(window, names))
            continue;
        const app = !window.is_override_redirect() && APP_TYPES.includes(type);
        if (!app && !actor.visible)
            continue;
        windows.push(describeWindow(window, actor, app));
    }
    return windows;
}

let warnedShellInternals = false;

// Log once that the Shell's banner or modal state could not be read.
function warnShellInternals(what) {
    if (warnedShellInternals)
        return;
    warnedShellInternals = true;
    console.warn(`Test Runner capture: cannot read the Shell's ${what}; ` +
        'reporting every monitor as covered');
}

// The Shell's own popups that may show other programs' text above windows:
// a notification banner and any modal Shell UI (dialogs, the overview, the
// lock screen). Fails closed: when the banner or modal state is missing on
// this Shell version or cannot be read, every monitor is reported as covered,
// so the runner refuses ``screen`` and takes windows only through
// CaptureWindow.
function shellOverlays() {
    const overlays = [];
    const pid = SHELL_PID;
    const add = rect => {
        if (rect.width > 0 && rect.height > 0) {
            overlays.push({
                id: `s${overlays.length + 1}`,
                kind: 'overlay',
                pid,
                title: '',
                frame: rect,
                minimized: false,
                focused: false,
                drawn: true,
                on_all_workspaces: true,
                workspace: -1,
            });
        }
    };
    const coverAll = () => {
        const display = global.display;
        for (let i = 0; i < display.get_n_monitors(); i++)
            add(rectObject(display.get_monitor_geometry(i)));
    };
    try {
        const banner = Main.messageTray?._bannerBin;
        if (!banner || typeof banner.get_n_children !== 'function') {
            warnShellInternals('notification banner');
            coverAll();
            return overlays;
        }
        if (banner.visible && banner.mapped && banner.get_n_children() > 0) {
            const [x, y] = banner.get_transformed_position();
            const [width, height] = banner.get_transformed_size();
            if (![x, y, width, height].every(Number.isFinite))
                throw new Error('banner geometry');
            add({
                x: Math.floor(x),
                y: Math.floor(y),
                width: Math.ceil(width),
                height: Math.ceil(height),
            });
        }
    } catch {
        warnShellInternals('notification banner');
        coverAll();
        return overlays;
    }
    try {
        const modalCount = Main.modalCount;
        const overview = Main.overview;
        if (!Number.isInteger(modalCount) || !overview ||
            typeof overview.visible !== 'boolean') {
            warnShellInternals('modal state');
            coverAll();
            return overlays;
        }
        if (modalCount > 0 || overview.visible)
            coverAll();
    } catch {
        warnShellInternals('modal state');
        coverAll();
    }
    return overlays;
}

function listWindowsJson() {
    const manager = global.workspace_manager;
    const display = global.display;
    let names = [];
    try {
        const settings = new Gio.Settings({schema_id: 'org.gnome.desktop.wm.preferences'});
        names = settings.get_strv('workspace-names');
    } catch {
        names = [];
    }
    const monitors = [];
    for (let i = 0; i < display.get_n_monitors(); i++)
        monitors.push(rectObject(display.get_monitor_geometry(i)));
    return JSON.stringify({
        version: HELPER_VERSION,
        workspace_count: manager.get_n_workspaces(),
        active_workspace: manager.get_active_workspace_index(),
        workspace_names: names,
        monitors,
        primary_monitor: display.get_primary_monitor(),
        // Topmost last.
        windows: [...listedWindows(), ...shellOverlays()],
    });
}

function runnerDir() {
    return GLib.build_filenamev([GLib.get_user_runtime_dir(), RUNNER_DIR_NAME]);
}

function currentUid() {
    return new Gio.Credentials().get_unix_user();
}

// The runner's folder: a real directory of this user, mode 0700.
function checkRunnerDir(dir, refused) {
    let info;
    try {
        info = Gio.File.new_for_path(dir).query_info(
            'standard::type,unix::uid,unix::mode',
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            null);
    } catch {
        throw refused;
    }
    if (info.get_file_type() !== Gio.FileType.DIRECTORY ||
        info.get_attribute_uint32('unix::uid') !== currentUid() ||
        (info.get_attribute_uint32('unix::mode') & 0o777) !== 0o700)
        throw refused;
}

function isPrivateFile(info) {
    return info.get_file_type() === Gio.FileType.REGULAR &&
        info.get_attribute_uint32('unix::uid') === currentUid() &&
        (info.get_attribute_uint32('unix::mode') & 0o777) === 0o600;
}

// The token the runner wrote: helper-token in its folder, a regular 0600
// file of this user (checked without following links, and again on the
// opened file).
function readToken() {
    const refused = new HelperError(ERROR_UNAUTHORIZED, 'caller not allowed');
    const dir = runnerDir();
    checkRunnerDir(dir, refused);
    const file = Gio.File.new_for_path(GLib.build_filenamev([dir, TOKEN_FILE_NAME]));
    const attributes = 'standard::type,unix::uid,unix::mode,unix::inode';
    let stream = null;
    try {
        const info = file.query_info(
            attributes, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
        if (!isPrivateFile(info))
            throw refused;
        stream = file.read(null);
        const opened = stream.query_info(attributes, null);
        if (!isPrivateFile(opened) ||
            opened.get_attribute_uint64('unix::inode') !==
                info.get_attribute_uint64('unix::inode'))
            throw refused;
        const bytes = stream.read_bytes(TOKEN_MAX_BYTES, null);
        const token = new TextDecoder().decode(bytes.toArray()).trim();
        if (!TOKEN_RE.test(token))
            throw refused;
        return token;
    } catch {
        throw refused;
    } finally {
        if (stream) {
            try {
                stream.close(null);
            } catch {
                // Closing a read-only stream; nothing else to do.
            }
        }
    }
}

function tokensEqual(given, expected) {
    if (typeof given !== 'string' || given.length !== expected.length)
        return false;
    let difference = 0;
    for (let i = 0; i < expected.length; i++)
        difference |= given.charCodeAt(i) ^ expected.charCodeAt(i);
    return difference === 0;
}

function logRefusal(invocation) {
    const sender = invocation.get_sender();
    if (!sender) {
        console.warn('Test Runner capture: refused a call from an unknown process');
        return;
    }
    Gio.DBus.session.call(
        'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
        'GetConnectionUnixProcessID', new GLib.Variant('(s)', [sender]),
        new GLib.VariantType('(u)'), Gio.DBusCallFlags.NONE, 1000, null,
        (connection, result) => {
            try {
                const [pid] = connection.call_finish(result).deepUnpack();
                console.warn(`Test Runner capture: refused a call from process ${pid}`);
            } catch {
                console.warn('Test Runner capture: refused a call from an unknown process');
            }
        });
}

// Refuse the call unless ``token`` is the runner's current token.
function requireCaller(invocation, token) {
    let allowed = false;
    try {
        allowed = tokensEqual(token, readToken());
    } catch {
        allowed = false;
    }
    if (!allowed) {
        logRefusal(invocation);
        throw new HelperError(ERROR_UNAUTHORIZED, 'caller not allowed');
    }
}

// The file to write, only directly inside the runner's private folder.
function checkedFile(path) {
    const refused = new HelperError(ERROR_REFUSED, 'path not allowed');
    if (typeof path !== 'string' || !path.startsWith('/'))
        throw refused;
    const dir = runnerDir();
    if (GLib.path_get_dirname(path) !== dir)
        throw refused;
    if (!FILE_NAME_RE.test(GLib.path_get_basename(path)))
        throw refused;
    checkRunnerDir(dir, refused);
    return Gio.File.new_for_path(path);
}

// A new file only (fails when the path exists, links included).
function createStream(file) {
    try {
        return file.create(Gio.FileCreateFlags.PRIVATE, null);
    } catch {
        throw new HelperError(ERROR_REFUSED, 'file exists or cannot be created');
    }
}

function removeQuietly(file) {
    try {
        file.delete(null);
    } catch {
        // Already gone; the runner removes its folder's files too.
    }
}

function findWindowActor(id) {
    for (const actor of global.get_window_actors()) {
        const window = actor.get_meta_window();
        if (window && String(window.get_id()) === id)
            return [window, actor];
    }
    throw new HelperError(ERROR_NOT_FOUND, 'window not found');
}

// The frame rect in the window actor's own coordinates.
function frameClip(window) {
    const frame = window.get_frame_rect();
    const buffer = window.get_buffer_rect();
    return new Mtk.Rectangle({
        x: frame.x - buffer.x,
        y: frame.y - buffer.y,
        width: frame.width,
        height: frame.height,
    });
}

// The window's own picture, written through the stream of the file this
// call created (never a second open of the path).
async function writeWindowContent(window, actor, file) {
    const clip = frameClip(window);
    let content = null;
    try {
        content = actor.paint_to_content(clip);
    } catch {
        content = null;
    }
    const texture = content ? content.get_texture() : null;
    if (!texture)
        throw new HelperError(ERROR_FAILED, 'no window image');
    const stream = createStream(file);
    try {
        await Shell.Screenshot.composite_to_stream(
            texture, 0, 0, -1, -1, 1, null, 0, 0, 1, stream);
    } catch {
        throw new HelperError(ERROR_FAILED, 'no window image');
    } finally {
        stream.close(null);
    }
}

class CaptureHelper {
    VersionAsync(_params, invocation) {
        try {
            invocation.return_value(new GLib.Variant('(u)', [HELPER_VERSION]));
        } catch (error) {
            answerError(invocation, error);
        }
    }

    ListWindowsAsync(params, invocation) {
        try {
            const [token] = params;
            requireCaller(invocation, token);
            invocation.return_value(new GLib.Variant('(s)', [listWindowsJson()]));
        } catch (error) {
            answerError(invocation, error);
        }
    }

    async CaptureAreaAsync(params, invocation) {
        let file = null;
        try {
            const [token, x, y, width, height, path] = params;
            requireCaller(invocation, token);
            if (![x, y, width, height].every(Number.isInteger) ||
                width < 1 || height < 1 ||
                width > MAX_AREA_SIDE || height > MAX_AREA_SIDE)
                throw new HelperError(ERROR_REFUSED, 'invalid area');
            file = checkedFile(path);
            const stream = createStream(file);
            try {
                const shooter = new Shell.Screenshot();
                await shooter.screenshot_area(x, y, width, height, stream);
            } finally {
                stream.close(null);
            }
            invocation.return_value(null);
        } catch (error) {
            if (file && !(error instanceof HelperError && error.dbusName === ERROR_REFUSED))
                removeQuietly(file);
            answerError(invocation, error);
        }
    }

    async CaptureWindowAsync(params, invocation) {
        let file = null;
        try {
            const [token, id, path] = params;
            requireCaller(invocation, token);
            if (typeof id !== 'string' || !/^[0-9]{1,20}$/.test(id))
                throw new HelperError(ERROR_NOT_FOUND, 'window not found');
            file = checkedFile(path);
            const [window, actor] = findWindowActor(id);
            await writeWindowContent(window, actor, file);
            invocation.return_value(null);
        } catch (error) {
            if (file && !(error instanceof HelperError && error.dbusName === ERROR_REFUSED))
                removeQuietly(file);
            answerError(invocation, error);
        }
    }

    ActiveWorkspaceAsync(params, invocation) {
        try {
            const [token] = params;
            requireCaller(invocation, token);
            const index = global.workspace_manager.get_active_workspace_index();
            invocation.return_value(new GLib.Variant('(i)', [index]));
        } catch (error) {
            answerError(invocation, error);
        }
    }

    ActivateWorkspaceAsync(params, invocation) {
        try {
            const [token, index] = params;
            requireCaller(invocation, token);
            const workspace = Number.isInteger(index) && index >= 0
                ? global.workspace_manager.get_workspace_by_index(index)
                : null;
            if (!workspace)
                throw new HelperError(ERROR_NOT_FOUND, 'workspace not found');
            // No focus request: the Shell restores focus as on any switch.
            workspace.activate(global.display.get_current_time_roundtrip());
            invocation.return_value(null);
        } catch (error) {
            answerError(invocation, error);
        }
    }
}

export default class TestRunnerCaptureExtension extends Extension {
    enable() {
        try {
            this._exported = Gio.DBusExportedObject.wrapJSObject(
                INTERFACE_XML, new CaptureHelper());
            this._exported.export(Gio.DBus.session, OBJECT_PATH);
            this._nameId = Gio.bus_own_name_on_connection(
                Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
        } catch (error) {
            console.error(`Test Runner capture: could not start: ${error}`);
            this.disable();
        }
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._exported) {
            try {
                this._exported.unexport();
            } catch {
                // Not exported; nothing to undo.
            }
            this._exported = null;
        }
    }
}
