import asyncio
import logging
import re
import ssl
from _ssl import PROTOCOL_SSLv23
from ssl import SSLContext

from stratus.event import Event, EventType
from stratus.helpers.dictionaries import CaseInsensitiveDict
from stratus.helpers.permissions import PermissionManager
from stratus.irc.state import Channel
from stratus.irc.protocol import IRCProtocol

logger = logging.getLogger("stratus")




class IRCClient:
    """
    An implementation of Connection for IRC.
    :type use_ssl: bool
    :type server: str
    :type port: int
    :type _connected: bool
    :type _ignore_cert_errors: bool
    """

    def __init__(self, bot, name, bot_nick, *, config, server, port=6667, use_ssl=False,
                 ignore_cert_errors=True, timeout=300):
        """
        :type bot: stratus.engine.Stratus
        :type name: str
        :type bot_nick: str
        :type config: dict[str, unknown]
        :type server: str
        :type port: int
        :type use_ssl: bool
        :type ignore_cert_errors: bool
        :type timeout: int
        """
        self.bot = bot
        self.loop = bot.loop
        self.name = name
        self.bot_nick = bot_nick

        self.channels = CaseInsensitiveDict()

        self.config = config

        # create permissions manager
        self.permissions = PermissionManager(self)

        self.waiting_messages = dict()

        self.use_ssl = use_ssl
        self._ignore_cert_errors = ignore_cert_errors
        self._timeout = timeout
        self.server = server
        self.port = port

        # create SSL context
        if self.use_ssl:
            self.ssl_context = SSLContext(PROTOCOL_SSLv23)
            if self._ignore_cert_errors:
                self.ssl_context.verify_mode = ssl.CERT_NONE
            else:
                self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        else:
            self.ssl_context = None

        # if we're connected
        self._connected = False
        # if we've quit
        self._quit = False

        # transport and protocol
        self._transport = None
        self._protocol = None

    def describe_server(self):
        if self.use_ssl:
            return "+{}:{}".format(self.server, self.port)
        else:
            return "{}:{}".format(self.server, self.port)

    @asyncio.coroutine
    def connect(self):
        """
        Connects to the IRC server, or reconnects if already connected.
        """
        # connect to the irc server
        if self._quit:
            # we've quit, so close instead (because this has probably been called because of EOF received)
            self.close()
            return

        if self._connected:
            logger.info("[{}] Reconnecting".format(self.name))
            self._transport.close()
        else:
            self._connected = True
            logger.info("[{}] Connecting".format(self.name))

        self._transport, self._protocol = yield from self.loop.create_connection(
            lambda: IRCProtocol(self), host=self.server, port=self.port, ssl=self.ssl_context)

        # send the password, nick, and user
        self.set_pass(self.config["connection"].get("password"))
        self.set_nick(self.bot_nick)
        self.cmd("USER", self.config.get('user', 'stratus'), "3", "*",
                 self.config.get('real_name', 'Stratus'))

    def quit(self, reason=None):
        if self._quit:
            return
        self._quit = True
        if reason:
            self.cmd("QUIT", reason)
        else:
            self.cmd("QUIT")

        # Log ourselves quitting
        quit_event = Event(bot=self.bot, conn=self, event_type=EventType.quit, nick=self.bot_nick)
        yield from self._process_quit(quit_event)

    def close(self):
        if not self._quit:
            self.quit()
        if not self._connected:
            return

        self._transport.close()
        self._connected = False

    def message(self, target, *messages, log_hide=None):
        for text in messages:
            self.cmd("PRIVMSG", target, text, log_hide=log_hide)

    def action(self, target, text, log_hide=None):
        self.ctcp(target, "ACTION", text, log_hide=log_hide)

    def notice(self, target, text, log_hide=None):
        self.cmd("NOTICE", target, text, log_hide=log_hide)

    def set_nick(self, nick):
        self.cmd("NICK", nick)

    def join(self, channel):
        if channel not in self.channels:
            self.cmd("JOIN", channel)
            self.channels[channel] = Channel(self.name, channel)

    def part(self, channel):
        if channel in self.channels:
            self.cmd("PART", channel)
            del self.channels[channel]

    def set_pass(self, password):
        if not password:
            return
        self.cmd("PASS", password)

    def ctcp(self, target, ctcp_type, text, log_hide=None):
        """
        Makes the bot send a PRIVMSG CTCP of type <ctcp_type> to the target
        :type ctcp_type: str
        :type text: str
        :type target: str
        """
        out = "\x01{} {}\x01".format(ctcp_type, text)
        self.cmd("PRIVMSG", target, out, log_hide=log_hide)

    def cmd(self, command, *params, log_hide=None):
        """
        Sends a raw IRC command of type <command> with params <params>
        :param command: The IRC command to send
        :param params: The params to the IRC command
        :type command: str
        :type params: (str)
        """
        params = list(params)  # turn the tuple of parameters into a list
        if params:
            params[-1] = ':' + params[-1]
            self.send("{} {}".format(command, ' '.join(params)), log_hide=log_hide)
        else:
            self.send(command, log_hide=log_hide)

    def send(self, line, log_hide=None):
        """
        Sends a raw IRC line
        :type line: str
        """
        if not self._connected:
            raise ValueError("Connection must be connected to irc server to use send")
        self.loop.call_soon_threadsafe(self._send, line, log_hide)

    def _send(self, line, log_hide):
        """
        Sends a raw IRC line unchecked. Doesn't do connected check, and is *not* threadsafe
        :type line: str
        """
        if log_hide is not None:
            logger.info("[{}] >> {}".format(self.name, line.replace(log_hide, "<hidden>")))
        else:
            logger.info("[{}] >> {}".format(self.name, line))
        asyncio.ensure_future(self._protocol.send(line), loop=self.loop)

    @property
    def connected(self):
        return self._connected

    @asyncio.coroutine
    def pre_process_event(self, event):
        yield from super().pre_process_event(event)
        if event.type is not EventType.message:
            return
        finished = []
        for (nick, chan, regex), futures in self.waiting_messages.items():
            if all(future.done() for future in futures):
                finished.append((nick, chan, regex))
                continue
            if nick is not None and event.nick.lower() != nick:
                continue
            if chan is not None and event.chan_name.lower() != chan:
                continue

            try:
                match = regex.search(event.content)
            except Exception as exc:
                for future in futures:
                    future.set_exception(exc)
                finished.append((nick, chan, regex))
            else:
                if match:
                    for future in futures:
                        future.set_result(match)
                    finished.append((nick, chan, regex))

        for key in finished:
            del self.waiting_messages[key]

    def wait_for(self, message, nick=None, chan=None):
        """
        Waits for a message matching a specific regex
        This returns a future, so it should be treated like a coroutine
        :type nick: str
        :type message: str | re.__Regex
        """
        if nick is not None:
            nick = nick.lower()
        if chan is not None:
            chan = chan.lower()
        future = asyncio.Future(loop=self.bot.loop)
        if not hasattr(message, "search"):
            message = re.compile(message)

        key = (nick, chan, message)

        if key in self.waiting_messages:
            # what?
            self.waiting_messages[key].append(future)

        self.waiting_messages[key] = [future]
        return future

    @asyncio.coroutine
    def cancel_wait(self, message, nick=None, chan=None):
        if nick is not None:
            nick = nick.lower()
        if chan is not None:
            chan = chan.lower()

        for test_nick, test_chan, test_message, future in self.waiting_messages:
            if test_nick == nick and test_chan == chan and test_message == message:
                future.cancel()

    @asyncio.coroutine
    def _process_channel(self, event):
        if event.chan_name is None or event.chan_name.lower() == event.nick.lower():
            return  # the rest of this just process on channels

        channel = self.channels.get(event.chan_name)
        if channel is None:
            if event.type is EventType.part and event.nick.lower() == self.bot_nick.lower():
                return  # no need to create a channel when we're just leaving it
            elif event.type is not EventType.join:
                logger.warning("First mention of channel {} was from event type {}".format(event.chan_name, event.type))
            elif event.nick.lower() != self.bot_nick.lower():
                logger.warning("First join of channel {} was {}".format(event.chan_name, event.nick))
            channel = Channel(self.name, event.chan_name)
            self.channels[event.chan_name] = channel

        event.channel = channel
        event.channels = [channel]

        if event.type is EventType.part:
            if event.nick.lower() == self.bot_nick.lower():
                del self.channels[event.chan_name]
                return
        elif event.type is EventType.kick:
            if event.target.lower() == self.bot_nick.lower():
                del self.channels[event.chan_name]
                return

        if event.type is EventType.message:
            yield from channel.track_message(event)
        elif event.type is EventType.join:
            yield from channel.track_join(event)
        elif event.type is EventType.part:
            yield from channel.track_part(event)
        elif event.type is EventType.kick:
            yield from channel.track_kick(event)
        elif event.type is EventType.topic:
            yield from channel.track_topic(event)
        elif event.irc_command == 'MODE':
            channel.track_mode(event)
        elif event.irc_command == '353':
            channel.track_353_channel_list(event)

    @asyncio.coroutine
    def _process_nick(self, event):
        if event.type is not EventType.nick:
            return

        if event.nick.lower() == self.bot_nick.lower():
            logger.info("[{}] Bot nick changed from {} to {}.".format(self.name, self.bot_nick, event.content))
            self.bot_nick = event.content

        event.channels.clear()  # We will re-set all relevant channels below
        for channel in self.channels.values():
            if event.nick in channel.users:
                yield from channel.track_nick(event)
                event.channels.append(channel)

    @asyncio.coroutine
    def _process_quit(self, event):
        if event.type is not EventType.quit:
            return

        event.channels.clear()  # We will re-set all relevant channels below
        for channel in self.channels.values():
            if event.nick in channel.users:
                channel.track_quit(event)
                event.channels.append(channel)

    @asyncio.coroutine
    def pre_process_event(self, event):

        yield from self._process_channel(event)
        yield from self._process_nick(event)
        yield from self._process_quit(event)