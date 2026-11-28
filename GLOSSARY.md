# Surf

Surf lets AI agents drive a real Chrome on the user's own profile, one named piece of browser work at a time.

## Language

**Thread**:
A named piece of browser work an agent owns, held in a Chrome window of its own.
_Avoid_: tab, window

**Popup**:
A page another page opened, through `window.open` or a link with a target, whether Chrome shows it as a window or a tab.
_Avoid_: new tab, child page

**Popup thread**:
A thread for a popup that a thread's page opened, directly or through its own popups, named after that thread plus a number, such as `app/1`.
_Avoid_: child thread, subthread
