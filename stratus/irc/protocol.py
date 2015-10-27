import asyncio
import logging
import re

from stratus.event import Event, EventType, IrcEvent

logger = logging.getLogger("stratus")

irc_prefix_re = re.compile(r":([^ ]*) ([^ ]*) (.*)")
irc_noprefix_re = re.compile(r"([^ ]*) (.*)")
irc_netmask_re = re.compile(r"([^!@]*)!([^@]*)@(.*)")
irc_param_re = re.compile(r"(?:^|(?<= ))(:.*|[^ ]+)")

irc_command_to_event_type = {
    'PRIVMSG': EventType.message,
    'JOIN': EventType.join,
    'PART': EventType.part,
    'KICK': EventType.kick,
    'NICK': EventType.nick,
    'QUIT': EventType.quit,
    '332': EventType.topic
}



class IRCProtocol(asyncio.Protocol):
    """
    :type loop: asyncio.events.AbstractEventLoop
    :type conn: IrcServer
    :type bot: stratus.engine.Stratus
    :type _input_buffer: bytes
    :type _connected: bool
    :type _transport: asyncio.transports.Transport
    :type _connected_future: asyncio.Future
    """

    def __init__(self, conn):
        """
        :type conn: IrcServer
        """
        self.loop = conn.loop
        self.bot = conn.bot
        self.conn = conn

        # input buffer
        self._input_buffer = b""

        # connected
        self._connected = False

        # transport
        self._transport = None

        # Future that waits until we are connected
        self._connected_future = asyncio.Future(loop=self.loop)

    def connection_made(self, transport):
        self._transport = transport
        self._connected = True
        self._connected_future.set_result(None)
        # we don't need the _connected_future, everything uses it will check _connected first.
        del self._connected_future

    def connection_lost(self, exc):
        self._connected = False
        # create a new connected_future for when we are connected.
        self._connected_future = asyncio.Future(loop=self.loop)
        if exc is None:
            # we've been closed intentionally, so don't reconnect
            return
        logger.info("[{}] Connection lost.".format(self.conn.name))
        asyncio.ensure_future(self.conn.connect(), loop=self.loop)

    def eof_received(self):
        self._connected = False
        # create a new connected_future for when we are connected.
        self._connected_future = asyncio.Future(loop=self.loop)
        logger.info("[{}] EOF received.".format(self.conn.name))
        asyncio.ensure_future(self.conn.connect(), loop=self.loop)
        return False

    @asyncio.coroutine
    def send(self, line):
        # make sure we are connected before sending
        if not self._connected:
            yield from self._connected_future
        line = line.splitlines()[0][:500] + "\r\n"
        data = line.encode("utf-8", "replace")
        self._transport.write(data)

    def data_received(self, data):
        self._input_buffer += data

        while b"\r\n" in self._input_buffer:
            line_data, self._input_buffer = self._input_buffer.split(b"\r\n", 1)
            line = line_data.decode()

            # parse the line into a message
            if line.startswith(":"):
                prefix_line_match = irc_prefix_re.match(line)
                if prefix_line_match is None:
                    logger.critical("[{}] Received invalid IRC line '{}' from {}".format(
                        self.conn.name, line, self.conn.describe_server()))
                    continue

                netmask_prefix, command, params = prefix_line_match.groups()
                netmask_match = irc_netmask_re.match(netmask_prefix)
                if netmask_match is None:
                    # This isn't in the format of a netmask
                    nick = netmask_prefix
                    user = None
                    host = None
                    mask = netmask_prefix
                else:
                    nick = netmask_match.group(1)
                    user = netmask_match.group(2)
                    host = netmask_match.group(3)
                    mask = netmask_prefix
            else:
                noprefix_line_match = irc_noprefix_re.match(line)
                if noprefix_line_match is None:
                    logger.critical("[{}] Received invalid IRC line '{}' from {}".format(
                        self.conn.name, line, self.conn.describe_server()))
                    continue
                command = noprefix_line_match.group(1)
                params = noprefix_line_match.group(2)
                nick = None
                user = None
                host = None
                mask = None

            command_params = irc_param_re.findall(params)

            # Reply to pings immediately

            if command == "PING":
                asyncio.ensure_future(self.send("PONG " + command_params[-1]), loop=self.loop)

            # Parse the command and params

            # Event type
            if command in irc_command_to_event_type:
                event_type = irc_command_to_event_type[command]
            else:
                event_type = EventType.other

            # Content
            if command_params and command_params[-1].startswith(":"):
                # If the last param is in the format of `:content` remove the `:` from it, and set content from it
                content = command_params[-1][1:]
            elif event_type is EventType.nick:
                content = command_params[0]
            else:
                content = None


            # Parse for CTCP
            if event_type is EventType.message and content.count("\x01") >= 2 and content.startswith("\x01"):
                # Remove the first \x01, then rsplit to remove the last one, and ignore text after the last \x01
                ctcp_text = str(content[1:].rsplit("\x01", 1)[0])  # str() to make python happy - not strictly needed
                ctcp_text_split = ctcp_text.split(None, 1)
                if ctcp_text_split[0] == "ACTION":
                    # this is a CTCP ACTION, set event_type and content accordingly
                    event_type = EventType.action
                    content = ctcp_text_split[1]
                else:
                    # this shouldn't be considered a regular message
                    event_type = EventType.other
            else:
                ctcp_text = None

            # Channel
            if command == "353":
                # 353 format is `:network.name 353 bot_nick = #channel :user1 user2`, if we just used the below,
                # we would think the channel was the bot_nick
                channel = command_params[2].lower()
            elif (command_params and (len(command_params) > 2 or not command_params[0].startswith(":"))
                  and event_type is not EventType.nick):

                if command_params[0].lower() == self.conn.bot_nick.lower():
                    # this is a private message - set the channel to the sender's nick
                    channel = nick.lower()
                else:
                    channel = command_params[0].lower()
            elif command == "JOIN":
                channel = content
            else:
                channel = None

            # Target (for KICK, INVITE)
            if event_type is EventType.kick:
                target = command_params[1]
            elif command == "INVITE":
                target = command_params[0]
            elif command == "MODE":
                if len(command_params) > 2:
                    target = command_params[2]
                else:
                    target = command_params[0]
                    channel = None
            else:
                target = None

            # Set up parsed message
            event = IrcEvent(bot=self.bot, conn=self.conn, event_type=event_type, content=content, target=target,
                             channel_name=channel, nick=nick, user=user, host=host, mask=mask, irc_raw=line,
                             irc_command=command, irc_command_params=command_params, irc_ctcp_text=ctcp_text)
            asyncio.ensure_future(self.conn.process(event))
