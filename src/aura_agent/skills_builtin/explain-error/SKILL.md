---
name: explain-error
description: Explain an error message or stack trace in plain words and say how to fix it, using the user's own code.
license: Apache-2.0
---
1. Read the whole message; find the error type, the message and the deepest frame that is in the user's code.
2. Open that line (read_file) and the lines around it.
3. Explain in two or three plain sentences what went wrong and why.
4. Give the fix for this code (or two options when the cause isn't certain) and how to confirm it worked.
5. If the error comes from a missing package or tool, give the install command and leave running it to the user.
