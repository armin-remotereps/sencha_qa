# Environment page: phone and tablet setup guide

Date: 2026-11-08
Plan: inline (template-only documentation change)

## What changed
- `templates/partials/phone_setup_tabs.html` (new): Android and iPhone & iPad setup tabs (`osTabs()`, ids prefixed `phone-`).
  - Host prerequisites: Node.js, Appium 3 and its driver, platform tools / Xcode, `ANDROID_HOME`.
  - Device settings: USB debugging; Developer Mode, Enable UI Automation and Zoom off on the iPhone.
  - Step-by-step WebDriverAgent signing in Xcode:
    - the Team ID from the certificate's `OU`, never the bracketed value of `find-identity`;
    - the WebDriverAgentRunner target, its own bundle identifier, Product → Test;
    - the trust step;
    - re-signing every 7 days with a free Apple ID.
  - The runner `.env` keys, the preinstalled-WDA option, and `diagnose --mobile`.
  - Browser settings for the `browse_*` tools.
- `templates/projects/environment.html`: a "Test a phone or tablet" card after "Set it up" (writable members only).

## Why
The user asked for Android and iOS setup documentation on the Environment page, including exactly how to sign WebDriverAgent.

## Behaviour and contract changes
- New card on the Environment page; static content, no context variables, no new view.

## Notes for future changes
- Keep the partial in sync with `controller_client/example.env` (the phone `CONTROLLER_*` keys), `mobile_ios.py` `_signing_capabilities` and `diagnose --mobile`.
- iOS `CONTROLLER_DEVICE` must be the hardware UDID (`xcrun xctrace list devices`), not CoreDevice's Identifier from `devicectl`.
- Not yet followed end to end on a real iPhone or Android phone.
