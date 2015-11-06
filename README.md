This is a Telegram bot which executes 
[OCaml](https://en.wikipedia.org/wiki/OCaml) code and returns 
its evaluation in a Telegram chat message.

Description
===========

This bot requires Python 3 (with 
[requests](http://docs.python-requests.org/en/latest/) module) and OCaml.

The program gets incoming messages from the Telegram server through the
[long polling](https://en.wikipedia.org/wiki/Long_polling) tecnique. 
It opens a ocaml shell for each chat, whose standard
input and output are accessible through a pipe. Incoming messages are
filtered to interpret bot commands and ignore instructions which may be a
security hazard, then each valid ocaml command is sent to the ocaml shell.

There is a couple of threads constantly running for each chat:

- one thread reads the output of the shell related to his chat and stores
  the output into a string variable;
- the other thread periodically sends the output back as a chat message.

The buffer variable is shared and synchronized between each couple of
threads.

Chats which are inactive by a certain time are closed automatically. A
thread periodically chechs for inactivity.

Run
=====
```bash
python ocaml_bot.py <bot_auth_token> [--log=debug|info|error] [--timeout=] 
[--logfile=]
```
The timeout switch sets the time before a chat is killed for inactivity.

License
=======
The project is licensed under GPL 3. See [LICENSE](./LICENSE)
file for the full license.
