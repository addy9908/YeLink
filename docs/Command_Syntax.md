# YeLink command syntax

A command is a sequence of key-value pairs separated by commas or spaces. Several commands can be chained in one message with a semicolon. The firmware treats commas, semicolons, spaces, and line ends as separators.

| Key | Meaning | Values |
|---|---|---|
| `p` | Digital output pin | 2 to 13 (required) |
| `v` | Output level | 1 = high, 0 = low (default 0; any non-zero number counts as 1) |
| `d` | Duration in seconds | Decimal values allowed; used only when `v=1` |

Examples:

| Command | Effect |
|---|---|
| `p=8,v=1` | Pin 8 goes high and stays high (latch) |
| `p=8,v=0` | Pin 8 goes low and cancels a running timer on that pin |
| `p=8,v=1,d=5` | Pin 8 goes high and returns to low after 5 s |
| `p=8,v=1,d=0.05` | A 50 ms pulse |
| `p=7,v=1;p=8,v=1` | Two pins switched by one message (the parts switch one after another, about 1.5 ms apart) |

`d=0`, or no `d`, with `v=1` latches the pin on. A new command with `v=0` cancels a running timer on that pin. `PING` is answered with `PONG`.

## Replies from the firmware

| Reply | Meaning |
|---|---|
| `READY` | Sent once after the board starts |
| `OK p=<pin> v=<level> d=<seconds>` | Command accepted, sent after the pin has been set |
| `ERR bad pin: <n>` | Missing or out-of-range pin; no output changes |
| `REVERT p=<pin> v=0` | A timed pulse ended and the pin returned to low |

A valid pin with a non-numeric duration (for example `d=abc`) is not rejected: the duration is read as zero, so the pin latches on. Check command strings with the manual command field of the adapter before a session.
