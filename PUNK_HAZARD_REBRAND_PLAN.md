# Punk Hazard Rebrand and UX Plan

## Status

Planning document for Claude. Do not begin Django integration until the standalone visual prototype has been reviewed and approved.

## Objective

Rebrand the user-facing product from **Auto Tester / Sencha QA** to **Punk Hazard** and redesign it as a guided AI testing workspace.

The work must happen in two intentionally separate stages:

1. Build and approve a standalone HTML/CSS prototype using mock data.
2. Integrate the approved design into the existing Django application without breaking project management, TestRail imports, controller connectivity, Celery tasks, WebSockets, test execution, logs, or screenshots.

## Confirmed Product Decisions

- The public product name is **Punk Hazard**.
- Use the existing logo assets:
  - `static/branding/punk-hazard-mark.png`
  - `static/branding/punk-hazard-logo.png`
- Add a project onboarding wizard.
- Replace the placeholder dashboard with an actionable dashboard.
- Complete visual prototyping before any backend or Django integration.
- Keep internal names such as the `auto_tester` Python package unchanged during the first integration pass. Technical renaming is a separate, higher-risk task.

## Brand Direction

### Logo assessment

Both supplied PNG files have genuine transparent backgrounds. The checkerboard seen in some image viewers is the transparency preview, not part of the artwork.

- `punk-hazard-mark.png`: 900 × 900, symbol only. Use it in the sidebar, compact navigation, loading screens, and small brand placements.
- `punk-hazard-logo.png`: 1254 × 1254, symbol plus wordmark. Use it on sign-in and other spacious brand surfaces. It is a square lockup, not a horizontal header logo.

Do not recolor, stretch, crop through, rotate, outline, or add strong shadows to either asset. Preserve aspect ratio and leave clear space around the mark.

### Colors extracted from the logos

| Token | Representative color | Intended use |
| --- | --- | --- |
| Brand navy | `#131D28` | Main background, dark text on bright brand colors |
| Electric cyan | `#2ECAE4` | Primary actions, links, focus states, active navigation |
| Hazard orange | `#FD5229` | Secondary accent, highlights, selected details |
| White | `#FDFDFD` | Primary text on dark surfaces |

The images contain gradients, so these values are representative anchors rather than replacements for the original artwork.

### Contrast rules

- Use navy text on cyan buttons. Navy/cyan contrast is approximately **8.66:1**.
- Do not use white text on cyan buttons; the contrast is approximately **1.93:1**.
- Use navy text on orange buttons. Navy/orange contrast is approximately **5.20:1**.
- Avoid white body-sized text on orange; the contrast is approximately **3.22:1**.
- Do not use hazard orange as the universal error color. Keep semantic red for failures, green for passed tests, amber for warnings, and blue/cyan for running or informational states.
- Verify every text/background combination against WCAG AA during prototype review.

### Proposed UI tokens

Create CSS custom properties rather than repeating raw color values:

```css
:root {
  --brand-navy: #131d28;
  --brand-cyan: #2ecae4;
  --brand-orange: #fd5229;
  --brand-white: #fdfdfd;

  --page: #0b1118;
  --surface: #131d28;
  --surface-raised: #1a2733;
  --surface-hover: #223240;
  --border: #2d3d4b;
  --text: #f5f8fa;
  --text-muted: #9baab6;

  --success: #2fbf71;
  --warning: #f2b84b;
  --danger: #ef5b62;
  --info: #4da3ff;
}
```

Treat these secondary values as prototype candidates. Adjust them after visual and contrast review.

### Visual character

The interface should feel energetic, technical, precise, and trustworthy. Draw from the geometry and split cyan/orange energy of the logo, but avoid turning every panel into a neon cyberpunk effect. Use the brand colors selectively so test results and warnings remain easy to interpret.

Recommended default: dark-first interface with restrained gradients and subtle geometric details. Do not implement a light theme in the first pass unless specifically requested.

## Product Language

Use familiar language in the main workflow and move implementation terminology into advanced diagnostics.

| Current wording | User-facing wording |
| --- | --- |
| Controller Client | Test Runner |
| Agent connected | Test machine connected |
| Agent System Info | Test Machine |
| OmniParser | Visual engine, shown only in advanced diagnostics |
| Project Prompt | Application Context |
| Test Runs | Runs |
| Uploads | Imports |
| Upload XML | Import from TestRail |
| Success | Passed |
| Started | Running |
| Waiting | Draft or Ready, depending on readiness |
| Force Disconnect | Disconnect test machine |
| API Key | Connection key, shown only in advanced setup |

Do not use playful Punk Hazard vocabulary for core actions. For example, keep “Run tests” rather than replacing it with an unclear metaphor.

## Target Information Architecture

### Global navigation

- Dashboard
- Projects
- User menu
  - Account
  - Sign out

### Project navigation

- Overview
- Test Cases
- Runs
- Environment
- Settings

The project overview should be the default destination when opening a project. TestRail import history can live inside Test Cases rather than appearing as a primary top-level destination.

Advanced information such as controller version, capabilities, OmniParser phase, weights directory, and connection keys belongs in Environment → Advanced diagnostics.

## Onboarding Wizard

The proposed four-step flow omits the machine connection required to execute tests. Preserve the requested sequence while adding that necessary step:

1. **Project details**
   - Project name
   - Optional tags
   - Explain what a project represents

2. **Application context**
   - Application URL
   - Platform or application type
   - Environment notes
   - Navigation hints
   - Authentication notes
   - AI-assisted refinement remains optional

3. **Add test cases**
   - Choose “Import from TestRail” or “Create manually”
   - Show file requirements before selection
   - In the integrated version, show an import preview and validation feedback

4. **Connect a test machine**
   - Explain why the runner is needed in plain language
   - Select operating system
   - Download the Test Runner
   - Show concise numbered setup instructions
   - Confirm connection with a clear live readiness state
   - Keep protocol and visual-engine details behind “Advanced diagnostics”

5. **First run**
   - Select cases
   - Name the run, with a sensible default
   - Show a readiness checklist
   - Start the run
   - Lead into the results page

### Wizard behavior for integration

- Redirect newly created projects to the wizard rather than back to the project list.
- Save each completed step before moving forward.
- Make the wizard resumable from the dashboard and project overview.
- Allow returning to completed steps without losing data.
- Only allow skipping truly optional fields.
- Provide a deliberate “Finish later” action.
- Derive as much completion as possible from existing project data:
  - Project details: project exists and has a name.
  - Application context: `project_prompt` is not blank.
  - Test cases: at least one test case exists.
  - First run: at least one test run exists.
- A machine being disconnected now must not erase the fact that setup was completed previously. If necessary, persist a `runner_setup_completed_at` timestamp after the first successful connection.
- Existing projects must not be forced through the wizard. Show a non-blocking setup checklist based on their derived state.

## Dashboard Definition

The dashboard must answer “What needs my attention?” and provide direct next actions.

### Recommended sections

1. **Welcome and primary action**
   - New project button
   - Continue setup button when applicable

2. **Attention required**
   - Failed recent runs
   - Disconnected test machines for projects with planned work
   - Incomplete project setup

3. **Run summary**
   - Runs in progress
   - Pass rate over a clear period
   - Passed and failed cases
   - Never-run cases

4. **Projects**
   - Recently accessed projects
   - Setup completion
   - Runner state
   - Latest run result
   - One clear primary action per card

5. **Recent activity**
   - Imported cases
   - Started or completed runs
   - Recent failures

Do not add decorative metrics that do not lead to an action or decision. Every card must have a meaningful destination.

## Stage 1 — Standalone HTML/CSS Prototype

### Purpose

Validate brand application, navigation, page hierarchy, responsive behavior, content, and workflow before touching Django integration.

### Hard boundary

During Stage 1, do not modify:

- Django models, migrations, views, forms, services, URLs, settings, or context processors
- Celery tasks or queues
- WebSocket consumers or controller protocol
- Agent or orchestrator code
- Existing functional templates
- Deployment, environment, Docker, Nginx, or domain configuration
- Internal package names

Use mock content only. No forms should submit to real endpoints, and no page should depend on Django template variables.

### Recommended prototype location

Create an isolated directory:

```text
ui-prototype/
  assets/
    prototype.css
  login.html
  dashboard.html
  onboarding-project.html
  onboarding-context.html
  onboarding-test-cases.html
  onboarding-runner.html
  onboarding-first-run.html
  project-overview.html
  test-cases.html
  runs.html
  run-detail.html
  environment.html
```

Reference the existing branding images from `../static/branding/`. Do not duplicate the source logo files.

Keep this prototype plain HTML and CSS. Use separate linked pages to demonstrate wizard progression. Do not add JavaScript unless the user separately approves small presentational interactions.

### Prototype page requirements

#### Sign-in

- Full Punk Hazard logo
- Clear value statement
- Email and password fields
- Helpful validation and error examples
- Obvious keyboard focus states

#### Dashboard

- Desktop sidebar and mobile header treatment
- Attention-required section
- Run summary
- Recent projects
- Recent activity
- New project and continue-setup actions

#### Onboarding pages

- Shared five-step progress indicator
- Clear title, explanation, and expected result for each step
- Back, save-and-continue, and finish-later actions
- Completed, current, and upcoming visual states
- Representative error, loading, empty, and success states

#### Project overview

- Setup progress
- Test-machine status
- Test-case count
- Latest run summary
- Clear recommended next action
- Project navigation

#### Test cases

- Search and filters
- Import and create actions
- Table at desktop widths
- Intentional small-screen alternative rather than an unusable horizontally compressed table
- Bulk selection and bulk action treatment
- Empty and validation states

#### Runs

- Named runs, date, status, case count, pass/fail summary, and duration placeholder
- Clear “New run” action
- Empty state leading back to test cases

#### Run detail

- Readiness checklist for draft runs
- Summary cards for completed/running runs
- Case results
- Screenshot preview
- Human-readable result summary
- Logs visually secondary to the result, not the first thing users must interpret

#### Environment

- Friendly connection state
- OS-specific Test Runner setup
- Last connected information
- Advanced diagnostics collapsed conceptually into a secondary panel

### Component inventory

Prototype reusable styles for:

- App shell and navigation
- Brand lockups
- Buttons and icon buttons
- Inputs, selects, textareas, checkboxes, and file upload
- Cards and stat cards
- Status badges
- Tables and mobile records
- Empty states
- Alerts and inline validation
- Progress steps and readiness checklist
- Dialog visual treatment
- Breadcrumbs
- Pagination
- Screenshot gallery
- Code/instruction blocks
- Skeleton/loading placeholders

Use semantic class names or component classes. Avoid copying long utility-class strings across every page in the prototype.

### Responsive requirements

- Design and verify at approximately 375 px, 768 px, 1024 px, and 1440 px widths.
- Navigation must have an intentional small-screen design.
- Primary actions must remain discoverable without horizontal scrolling.
- Tables must have a defined mobile strategy.
- Avoid fixed widths that assume a desktop viewport.

### Accessibility requirements

- Semantic headings and landmarks.
- Every form control has a visible label.
- Visible `:focus-visible` states.
- AA contrast for normal text and meaningful controls.
- Do not communicate status using color alone.
- Provide accessible names for icon-only controls.
- Dialog designs must anticipate focus trapping and Escape behavior during integration.
- Respect reduced-motion preferences.
- Logo images must have purposeful alternative text; decorative repeats should use empty alt text.

### Stage 1 acceptance gate

Do not begin integration until the user has reviewed the prototype and explicitly approved:

- Brand direction and color balance
- Navigation structure
- Dashboard composition
- Five-step onboarding flow
- Desktop and mobile layouts
- Core terminology
- Page-level hierarchy and primary actions

Capture requested revisions in the prototype first. The prototype is the visual source of truth for integration.

## Stage 2 — Django Integration

Begin only after Stage 1 approval.

### Phase 2.1: Design foundation and rebrand

- Add brand CSS and shared design tokens under Django static assets.
- Convert approved prototype components into shared Django template partials.
- Replace visible “Auto Tester” text with “Punk Hazard.”
- Add the mark to global navigation and the full lockup to sign-in.
- Add meaningful page titles to every primary template.
- Build responsive global and project navigation.
- Keep internal `auto_tester` names unchanged.
- Preserve all existing URLs initially unless changing one materially improves the new information architecture.

### Phase 2.2: Dashboard integration

- Create a dashboard service that calculates user-scoped summaries; keep query/business logic out of the view.
- Provide incomplete setup, recent projects, active/recent runs, failures, and activity data.
- Prevent N+1 queries with annotations and prefetching.
- Link every dashboard item to an existing or newly approved workflow.
- Add dashboard tests for authorization, empty state, and populated state.

### Phase 2.3: Onboarding integration

- Change project creation success flow to enter onboarding.
- Add wizard routes, thin views, forms, and service-layer operations.
- Persist only the state that cannot be reliably derived.
- Preserve unfinished form values and surface validation errors inline.
- Connect existing TestRail processing and WebSocket progress to the approved onboarding screen.
- Connect existing runner ZIP generation and live agent status to the runner step.
- Gate the first run with a readiness check and actionable explanations.
- Keep advanced controller and visual-engine diagnostics available but secondary.
- Add authorization and state-transition tests.

### Phase 2.4: Project workspace integration

- Make project overview the default project page.
- Move application context editing and environment information into their approved locations.
- Integrate approved Test Cases, Runs, Run Detail, and Environment layouts.
- Preserve real-time upload, run, case-log, screenshot, and runner-status updates.
- Replace silent invalid redirects with inline errors or messages.
- Add confirmations for destructive actions that currently lack them.
- Retain pagination, filtering, bulk actions, copying cases, resetting runs, aborting runs, and screenshot lightbox behavior.

### Phase 2.5: Product polish

- Add useful empty, loading, offline, reconnecting, success, and error states.
- Ensure “Start run” explains all blockers before dispatching work.
- Add user-defined run names if approved; this requires a model migration.
- Add rerun-failed and duplicate-run actions only after the core redesign is stable.
- Review copy for consistent terminology.

### Phase 2.6: Technical rebrand, separately approved

Treat the following as a later deployment task rather than bundling it into UI integration:

- Repository or directory rename
- `auto_tester` Python package rename
- Domain and TLS certificate changes
- Docker service/container names
- Environment variable naming
- Controller ZIP/module rename
- Monitoring credentials and deployment metadata
- Historic documentation references

This phase requires a deployment and rollback plan.

## Run Readiness Rules

The redesigned UI should not present a run as ready when execution will immediately wait or fail.

Before starting, show whether:

- At least one test case is selected.
- The test machine is connected.
- The visual engine is ready.
- No other run is active for the project.
- Application context is present or intentionally skipped.
- The run has a valid name if named runs are implemented.

Each failed check must explain how to resolve it and link to the appropriate step.

## Error and Trust Improvements

Include these in integration rather than treating them as optional polish:

- Show project and test-case form errors instead of silently redirecting.
- Show server-side TestRail validation errors; client-side inspection is not sufficient.
- Confirm test-case deletion and project archiving.
- Make destructive actions visually distinct from brand-orange accents.
- Do not encourage plain-text credentials in Application Context. For the first pass, provide a warning and safer copy. Design protected project secrets as a separate future feature.
- Hide API keys by default and keep them out of the main project overview.
- Describe machine and model failures in plain language before exposing diagnostic details.

## Verification Strategy

### Prototype verification

- Manually inspect every prototype page at the four target widths.
- Check keyboard navigation and visible focus order.
- Check contrast for brand and semantic colors.
- Verify logo clarity on all surfaces and sizes used.
- Verify that each page has one obvious primary action.
- Review the full first-project journey without relying on explanatory documentation.

### Integration verification

- Run scoped Django tests for `accounts`, `dashboard`, and `projects`.
- Add tests for new services, wizard access, wizard state, redirects, and dashboard queries.
- Verify existing TestRail upload processing and WebSocket updates.
- Verify controller connection, readiness state, start/abort/reset behavior, logs, and screenshots.
- Test with no projects, incomplete projects, disconnected runners, empty test suites, active runs, completed runs, and failed runs.
- Run type checks and formatting according to repository guidance.
- Perform a final visual comparison against the approved prototype.

## Recommended Delivery Order

1. Confirm this plan and unresolved wording choices.
2. Build shared prototype tokens and app shell.
3. Build sign-in and dashboard prototypes.
4. Build all five onboarding screens.
5. Build project overview and environment prototypes.
6. Build test-case, runs, and run-detail prototypes.
7. Complete responsive and accessibility review.
8. Revise until the user explicitly approves Stage 1.
9. Integrate the design foundation and user-facing rebrand.
10. Integrate the dashboard.
11. Integrate onboarding.
12. Integrate the project workspace and real-time behaviors.
13. Complete regression, accessibility, and visual verification.
14. Plan technical/deployment renaming separately if desired.

## Decisions to Confirm During Prototype Review

These do not block starting the static prototype, but must be settled before integration:

- Final tagline. Working copy: **“AI testing on real applications.”**
- Whether the downloadable component is called **Punk Hazard Test Runner** or simply **Test Runner**. Recommended: use “Test Runner” in navigation and “Punk Hazard Test Runner” in downloads.
- Whether named runs are part of the first integration or a follow-up migration.
- Whether protected project secrets are in scope now or handled as a separate security feature.
- Whether a light theme is desired later. Recommended: dark theme only for the first release.

## Definition of Done

The rebrand is complete when:

- All approved user-facing surfaces consistently use Punk Hazard branding.
- A new user can create a project and reach their first run through a guided, resumable flow.
- The dashboard clearly communicates attention items and next actions.
- Normal users do not need to understand controller protocols or OmniParser to operate the product.
- Run blockers are visible and actionable before execution begins.
- Existing project, import, execution, real-time update, result, log, and screenshot behavior remains functional.
- Primary workflows are responsive, keyboard-accessible, and visually consistent with the approved prototype.
- The internal technical rename remains isolated unless separately approved.
