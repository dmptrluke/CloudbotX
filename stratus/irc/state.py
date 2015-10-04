import asyncio
from collections import deque
import datetime
import logging
import re
import itertools

from stratus.event import EventType
from stratus.helpers.dictionaries import CaseInsensitiveDict


logger = logging.getLogger("stratus")

mode_re = re.compile(r'([@&~%\+]*)([a-zA-Z0-9_\\\[\]\{\}\^`\|][a-zA-Z0-9_\\\[\]\{\}\^`\|-]*)')
symbol_to_mode = {
    '+': 'v',
    '@': 'o'
}

history_key = "stratus:connections:{}:channels:{}:history"


def grouper(iterable, n, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)


class Channel:
    """
    name: the name of this channel
    users: A dict from nickname to User in this channel
    user_modes: A dict from User to an str containing all of the user's modes in this channel
    history: A list of (User, timestamp, message content)
    :type connection: str
    :type name: str
    :type users: dict[str, User]
    """

    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.users = CaseInsensitiveDict()
        self.history = deque(maxlen=100)
        self.topic = ""

    @property
    def _db_key(self):
        return history_key.format(self.connection.lower(), self.name.lower())

    @asyncio.coroutine
    def _add_history(self, event, *variables):
        """
        Adds an event to this channels history
        :type event: stratus.event.Event
        """
        to_store = '\n'.join(itertools.chain((event.type.name,), (str(v) for v in variables)))
        score = (datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(0)).total_seconds()
        yield from event.async(event.db.zadd, self._db_key, **{to_store: score})

    @asyncio.coroutine
    def get_history(self, event, min_time, with_timestamps=False):
        """
        :type event: stratus.event.Event
        :type min_time: datetime.datetime
        :rtype: [(EventType, str, str)] | [(datetime.datetime, EventType, str, str)]
        :param event: An event to access database from
        :param min_time: The minimum time to get history from
        :return: List of (Type, nickname, other data),
                    or list of (timestamp, nickname, other data) if with_timestamps=True
        """
        min_score = (min_time - datetime.datetime.utcfromtimestamp(0)).total_seconds()
        max_score = "+inf"
        raw_result = yield from event.async(event.db.zrangebyscore, self._db_key, min_score, max_score,
                                            withscores=with_timestamps)

        if with_timestamps:
            return [_parse_history_data(data, score) for data, score in grouper(raw_result, 2)]
        else:
            return [_parse_history_data(data) for data in raw_result]

    @asyncio.coroutine
    def track_message(self, event):
        """
        Adds a message to this channel's history, adding user info from the message as well
        :type event: stratus.event.Event
        """
        user = self.users[event.nick]
        if not user.mask_known:
            user.ident = event.user
            user.host = event.host
            user.mask = event.mask
        yield from self._add_history(event, user.nick, event.content)
        self.history.append((EventType.message, user.nick, datetime.datetime.utcnow(), event.content))

    @asyncio.coroutine
    def track_join(self, event):
        """
        :type event: stratus.event.Event
        """
        self.users[event.nick] = User(event.nick, ident=event.user, host=event.host, mask=event.mask, mode='')
        yield from self._add_history(event, event.nick)

    @asyncio.coroutine
    def track_part(self, event):
        """
        :type event: stratus.event.Event
        """
        del self.users[event.nick]
        yield from self._add_history(event, event.nick, event.content)

    @asyncio.coroutine
    def track_quit(self, event):
        """
        :type event: stratus.event.Event
        """
        del self.users[event.nick]
        yield from self._add_history(event, event.nick, event.content)

    @asyncio.coroutine
    def track_kick(self, event):
        """
        :type event: stratus.event.Event
        """
        del self.users[event.target]
        yield from self._add_history(event, event.nick, event.target, event.content)

    @asyncio.coroutine
    def track_nick(self, event):
        user = self.users[event.nick]

        if not user.mask_known:
            user.ident = event.user
            user.host = event.host
            user.mask = event.mask

        user.nick = event.content
        self.users[event.content] = user

        del self.users[event.nick]

    @asyncio.coroutine
    def track_topic(self, event):
        """
        :type event: stratus.event.Event
        """
        user = self.users[event.nick]
        if not user.mask_known:
            user.ident = event.user
            user.host = event.host
            user.mask = event.mask
        self.topic = event.content
        yield from self._add_history(event, user.nick, event.content)

    def track_mode(self, event):
        """
        IRC-specific tracking of mode changing
        :type event: stratus.event.Event
        """
        user = self.users[event.target]
        mode_change = event.irc_command_params[1]  # in `:Dabo!dabo@dabo.us MODE #obr +v obr`, `+v` is the second param
        if mode_change[0] == '-':
            user.mode = user.mode.replace(mode_change[1], '')  # remove the mode from the mode string
        elif mode_change[0] == '+':
            if mode_change[1] not in user.mode:
                user.mode += mode_change[1]  # add the mode to the mode string
        else:
            logger.warning("Invalid mode string '" + mode_change + "' found, ignoring.")

    def track_353_channel_list(self, event):
        for user in event.content.split():
            match = mode_re.match(user)
            if match is None:
                logger.warning("User mode {} didn't fit specifications.".format(user))
            # find mode
            mode_symbols = match.group(1)
            mode = ''
            if mode_symbols is not None:
                for symbol in mode_symbols:
                    symbol_mode = symbol_to_mode.get(symbol)
                    if symbol_mode is not None:
                        mode += symbol_mode
            # create user
            nick = match.group(2)
            user = User(nick, mode=mode)
            self.users[nick] = user


class User:
    """
    :param nick: The nickname of this User
    :param ident: The IRC ident of this User, if applicable
    :param host: The hostname of this User, if applicable
    :param mask: The IRC mask (nick!ident@host), if applicable
    :param mode: The IRC mode, if applicable
    :type nick: str
    :type ident: str
    :type host: str
    :type mask: str
    :type mask_known: bool
    :type mode: str
    """

    def __init__(self, nick, *, ident=None, host=None, mask=None, mode=''):
        self.nick = nick
        self.ident = ident
        self.host = host
        self.mask = mask
        self.mask_known = mask is not None
        self.mode = mode


def _parse_history_data(data, score=None):
    split = data.decode().split("\n")
    try:
        event_type = getattr(EventType, split[0])
    except AttributeError:
        event_type = EventType.other
    if score:
        return (score, event_type,) + tuple(split[1:])
    else:
        return (event_type,) + tuple(split[1:])