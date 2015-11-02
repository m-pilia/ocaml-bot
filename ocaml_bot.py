#!/usr/bin/env python3

# Copyright (C) 2015 Martino Pilia <martino.pilia@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
# Usage:
#   python ocaml_bot.py <bot_auth_token> [--log=debug|info|error]
#                                        [--logfile=]
#                                        [--timeout=]
#
# This is a Telegram bot capable of interpreting OCaml code. Requires Python 3
# (with requests module) and ocaml.
#
# The program gets incoming messages from the Telegram server through long
# polling tecnique. It opens a ocaml shell for each chat, whose standard
# input and output are accessible through a pipe. Incoming messages are
# filtered to interpret bot commands and filter security hazards, then each
# valid ocaml command is sent to the ocaml shell.
# There are two threads constantly running for each chat:
# - one thread reads the output of the shell related to his chat and stores
#   the output into a string variable;
# - the other thread periodically sends the output back as a chat message.
# The buffer variable is shared and synchronized between each couple of
# threads.
#
# Written by Martino on 2015-10-26
"""

import logging
import os
import re
import requests
import shlex
import signal
import sys
from subprocess import Popen, PIPE
import threading
import time

# Timeout before an inactive chat is closed
_TIMEOUT = 86400 # default: 1 day

# Time between two calls of inactive chats killer.
_TIMEOUT_KILLING_INTERVAL = 1800 # half hour

# Level for the log.
logLevel = logging.NOTSET

# Output file name for the log.
logFile = None

# Search for other switches.
if len(sys.argv) > 2:
    for arg in sys.argv:
        # set log level
        match = re.match("--log=([a-z]*)", arg)
        if match:
            level = match.group(1)
            if level == "debug":
                logLevel = logging.DEBUG
            elif level == "info":
                logLevel = logging.INFO
            elif level == "error":
                logLevel = logging.ERROR

        # set log file
        match = re.match("--logfile=([a-zA-Z0-9/._-]*)", arg)
        if match:
            logFile = match.group(1)

        # set chat timeout
        match = re.match("--timeout=([0-9]*)", arg)
        if match:
            _TIMEOUT = int(match.group(1))

# Set log options.
logging.basicConfig(level=logLevel, filename=logFile)

# Get bot token from first arg.
token = ""
if len(sys.argv) < 2 or re.match("--", sys.argv[1]):
    print("usage: ocaml_bot <bot_auth_token> " +
          "[--log=debug|info|error] [--logfile=] [--timeout=]")
    exit(1)
else:
    token = sys.argv[1]

# Bot address.
baseAddr = ("https://api.telegram.org/bot" + token)

# Command to open the OCaml shell.
# TERM='console' ensures no format sequence is added to the interpreter output.
ocamlArgs = "TERM='console' ocaml -noprompt -nopromptcont"

# Regex to parse a request for the interpreter.
instruction = re.compile("^/ml ([\s\S]*)$")

# Regex to match potentially harmful instructions.
hazard = re.compile(".*([Ss]ys|[Uu]nix|[Ss]tream|fork|exec|" +
                        "#\s*cd|#\s*directory|#\s*install_printer|" +
                        "fprintf|input_file|output_file|open_in|open_out).*")

# Memorize last update_id value, used for the offset parameter.
# Items are discarded by the server when the offset parameter is greater than
# their update_id.
lastUpdateId = 0

# Open chats. A dictionary contaning one other dictionary for each chat.
# Each dictionary contains some objects defining the chat status.
chats = {}

# Lock for concurrent access on the `chats` dictionary (a dict of dicts).
chatsLock = threading.Lock()

# Keys for dictionary objects representing a chat status.
_PIPE = 1 # Popen object for the ocaml shell
_MSG  = 2 # Text read from the pipe and ready to be sent.
_LOCK = 3 # Lock object for the chat status objects.
_LAST = 4 # Timestamp of the last received command.
_COND = 5 # Exit condition for the chat threads.
_READ_THREAD = 6 # Thread reading OCaml shell output.
_SEND_THREAD = 7 # Thread sending answer messages.
_ID = 8 # Chat id.

def sendMessage(chatId, msg):
    """ Send `msg` to the `chatId` chat.
    Parameters:
        chatId - id of the chat
        msg    - message to be sent
    """
    try:
        a = requests.post(url=
                baseAddr +
                "/sendMessage" +
                "?chat_id=%s" % (chatId) +
                "&text=%s" % (requests.utils.quote(msg)),
                timeout=120)
    except Exception as e:
        logging.exception(e)
        # retry after some time (tail recursive)
        time.sleep(5)
        sendMessage(chatId, msg)

def evaluate(chatId, s):
    """ Evaluate OCaml code and write the output in the pipe.
    Parameters:
        chatId - id of the chat
        s      - string to be evaluated by the OCaml shell
    """
    p = None
    try:
        chatsLock.acquire()
        p = chats[chatId][_PIPE]
        chatsLock.release()
    except KeyError as e:
        logging.exception(e)
        return

    try:
        p.stdin.write((s + "\n").encode('utf-8'))
        p.stdin.flush()
    except BrokenPipeError as e:
        logging.exception(e)
        # open new OCaml shell if the previous one was dead
        p = Popen(
            ocamlArgs,
            stdin = PIPE,
            stdout = PIPE,
            stderr = PIPE,
            bufsize = 1,
            shell = True,
            preexec_fn = os.setsid)
        # update dictionary
        chatsLock.acquire()
        chats[chatId][_PIPE] = p
        chatsLock.release()
        # resend command to the shell
        p.stdin.write(s.encode('utf-8'))
        p.stdin.flush()

def readResult(chatId):
    """ Read the output of the OCaml shell and store it into a string.
    Parameters:
        chatId - id of the chat
    """
    while True:
        try:
            # read line
            line = p.stdout.readline()

            chatsLock.acquire()
            chats[chatId][_LOCK].acquire() # critical section start
            # check condition for thread termination
            if chats[chatId][_COND]:
                chats[chatId][_LOCK].release()
                chatsLock.release()
                logging.debug("reading thread for chat %d exiting" % (chatId))
                return
            # add line to the buffer
            text = chats[chatId][_MSG]
            chats[chatId][_MSG] = text + line.decode('utf-8')
            chats[chatId][_LOCK].release() # critical section end
            chatsLock.release()
        except KeyError as e:
            logging.exception(e)
            continue

def sendAnswer(chatId):
    """ Send answer messages.
    Parameters:
        chatId - id of the chat
    """
    while True:
        try:
            chatsLock.acquire()
            chats[chatId][_LOCK].acquire() # critical section start
            # check condition for thread termination
            if chats[chatId][_COND]:
                chats[chatId][_LOCK].release()
                chatsLock.release()
                logging.debug("sender thread for chat %d exiting" % (chatId))
                return
            # read message buffer for the chat and clear it after
            msg = chats[chatId][_MSG]
            chats[chatId][_MSG] = ""
            chats[chatId][_LOCK].release() # critical section end
            chatsLock.release()
        except Exception as e:
            logging.exception(e)
            pass
        # send message
        if msg != "":
            sendMessage(chatId, msg)
        # sleep
        time.sleep(1)

def clearChat(chatId):
    """ Close the OCaml shell related to the chat, kill the threads and remove
    the chat from the chats dictionary.
    """
    chat = None
    try:
        chatsLock.acquire()
        chat = chats[chatId]
        chatsLock.release()
    except KeyError as e:
        logging.exception(e)
        return

    # close ocaml shell process
    p = chat[_PIPE]
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(2)
    except Exception as e:
        logging.exception(e)
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()

    # set condition for thread termination
    chat[_COND] = True

    # wait till thread termination
    try:
        chat[_READ_THREAD].join(2)
        chat[_SEND_THREAD].join(2)
    except Exception as e:
        logging.exception(e)
        pass

    # remove chat from the dictionary of open chats
    chatsLock.acquire()
    chats.pop(chatId, None)
    chatsLock.release()

    logging.debug("chat %d deletion complete" % (chatId))

def chatTimeoutKiller():
    """ Destroy inactive chats.
    """
    while True:
        time.sleep(_TIMEOUT_KILLING_INTERVAL)
        logging.info("Running chatTimeoutKiller")

        t = time.time()
        # filter inactive chats
        chatsLock.acquire()
        try:
            inact = [c for (k, c) in chats.items() if t - c[_LAST] > _TIMEOUT]
        except Exception as e:
            logging.exception(e)
            chatsLock.release()
            continue
        chatsLock.release()

        # clear inactive chats
        for chat in inact:
            clearChat(chat[_ID])

        logging.info("Finished chatTimeoutKiller")

### Application entry point

# Launch a thread which periodically kills the inactive chats.
t = threading.Thread(target=chatTimeoutKiller)
t.start()

# Check incoming messages
while True:
    r = None
    # Get messages (long polling)
    try:
        r = requests.post(url=
                baseAddr +
                "/getUpdates" +
                "?timeout=120" +
                "&offset=%d" % (lastUpdateId + 1),
                timeout=200).json()
    except Exception as e:
        logging.exception(e)
        time.sleep(5)
        continue # network problem?

    if not r["ok"]:
        continue # something amiss in the server

    # Process messages
    for u in r["result"]:
        if u["update_id"] > lastUpdateId:

            try:
                lastUpdateId = u["update_id"] # update offset value
                msg = u["message"]["text"]
                chatId = u["message"]["chat"]["id"]
            except Exception as e:
                logging.exception(e)
                continue

            # create new ocaml shell if needed for a new chat
            chatsLock.acquire()
            if chatId not in chats.keys():
                # open ocaml shell, comunicating with pipes for I/O
                p = Popen(
                        ocamlArgs,
                        stdin = PIPE,
                        stdout = PIPE,
                        stderr = PIPE,
                        bufsize = 1,
                        shell = True,
                        preexec_fn = os.setsid)

                # add chat to the dictionary
                chats[chatId] = {
                    _ID: chatId,
                    _PIPE: p,
                    _MSG: "",
                    _LOCK: threading.Lock(),
                    _COND: False
                }

                # create thread to read shell's output
                chats[chatId][_READ_THREAD] = threading.Thread(
                        target=readResult,
                        args=[chatId])
                chats[chatId][_READ_THREAD].start()

                # create thread to send message answers
                chats[chatId][_SEND_THREAD] = threading.Thread(
                        target=sendAnswer,
                        args=[chatId])
                chats[chatId][_SEND_THREAD].start()

            # update timestamp for last received message
            chats[chatId][_LAST] = time.time()
            
            chatsLock.release()

            if re.match("^/help.*", msg):
                # show help
                res = ("Hi. I am a very boring bot, who likes to talk in " +
                       "OCaml only. My available commands are:\n"
                       "  /help      - show this help message\n" +
                       "  /kill      - close the OCaml shell in use\n" +
                       "  /ml [code] - parse OCaml code (code can continue " +
                       "               between many \ml commands)")
                sendMessage(chatId, res)
                continue

            elif re.match("^/kill.*", msg):
                # close the chat and destroy its resources
                clearChat(chatId)
                continue

            match = instruction.match(msg)
            if not match:
                continue # no command for the bot
            else:
                msg = match.group(1) # get message content

            # fiter potentially dangerous intructions
            match = hazard.match(msg)
            if match:
                res = ("Sorry, your code seems to contain a " +
                       "forbidden identifier: %s" % match.group(1))
                sendMessage(chatId, res)
                continue

            # send useful part of the message to the ocaml shell
            evaluate(chatId, msg)
