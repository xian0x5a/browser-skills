# 1Password autofill

Use this when logging in with the configured 1Password extension. For initial configuration, read [1Password setup](1password-setup.md). Keep passwords, cookies, and other secrets out of printed output.

1Password shows two panels inside the page: the inline menu under a focused sign-in field, and the "Sign in with a passkey" popup. Snapshots don't list their entries and refs can't reach them, but screenshots show both, so select entries with `click_at`.

## Sign in

Click the sign-in field, or its label if the label covers it; the click brings up the 1Password menu. The snapshot then shows the status `1Password menu is available`; without it, 1Password isn't available on this page, so skip the screenshot and follow [Login and human unblock](../SKILL.md#login-and-human-unblock). Otherwise take a screenshot, look for the menu or the passkey popup, and `click_at` the entry for the task's account, preferring the passkey popup when both show. Selecting an entry starts a fill, a submit or a passkey check that finishes later, so wait or poll until the page settles, then check the snapshot; a filled password field shows `••••••••`. Signed in: done. Otherwise clear the login steps the snapshot shows, such as a password field or a Next button. Take another screenshot only when none remain: the next step is then likely a passkey popup, which only a screenshot shows.

```python
thread.click("@e7")  # sign-in field from the snapshot
thread.emit(thread.snapshot())  # look for the 1Password menu status
thread.screenshot("/tmp/surf-login.png")
# view the screenshot, then click the entry for the task's account
thread.click_at(1158, 449)
thread.sleep(3000)
thread.emit(thread.snapshot())  # signed in? else clear visible steps, then screenshot
```

- If several entries could match and the task doesn't say which, ask the user.
- Coordinates are in the screenshot file's pixels; if your view of the image is downscaled, scale the point back up.
- Without image viewing, `ArrowDown` then `Enter` in the inline menu fills its first entry; use this only when any saved login suits the task.
- If no panel appears, 1Password is locked, or the account has no entry, follow [Login and human unblock](../SKILL.md#login-and-human-unblock).
