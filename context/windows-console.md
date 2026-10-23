# Windows console

## QuickEdit mode is disabled at startup

**Id:** 313c44f1-c416-455b-b644-e5e19857249f
**Type:** workaround
**Status:** active
**Evidence:** confirmed
**Source:** commit f850eee (PR #115)
**Revisit when:** the worker stops running in an interactive Windows console, or console handling moves out of `croupier.main`

Croupier clears the console's QuickEdit flag when it starts on Windows.

**Reason:** a stray click inside the console window starts a text selection, which blocks writes to stdout and silently freezes the service until a key is pressed. On branch computers nobody is watching the console, so a frozen worker simply stops printing receipts.

**Consequence:** the console input mode is shared with the parent shell, so the original mode is remembered and restored at exit. The console is opened explicitly as `CONIN$` because the standard input handle may be a redirected file or pipe. Failures of the Win32 calls are logged and startup continues rather than aborting.

**Rejected alternative:** alternatives to clearing the flag (for example running without a console window) were not recorded; unknown.
