"""
    pyte.screens
    ~~~~~~~~~~~~

    This module provides classes for terminal screens, currently
    it contains three screens with different features:

    * :class:`~pyte.screens.Screen` -- base screen implementation,
      which handles all the core escape sequences, recognized by
      :class:`~pyte.streams.Stream`.
    * If you need a screen to keep track of the changed lines
      (which you probably do need) -- use
      :class:`~pyte.screens.DiffScreen`.
    * If you also want a screen to collect history and allow
      pagination -- :class:`pyte.screen.HistoryScreen` is here
      for ya ;)

    .. note:: It would be nice to split those features into mixin
              classes, rather than subclasses, but it's not obvious
              how to do -- feel free to submit a pull request.

    :copyright: (c) 2011-2012 by Selectel.
    :copyright: (c) 2012-2017 by pyte authors and contributors,
                    see AUTHORS for details.
    :license: LGPL, see LICENSE for more details.
"""
from __future__ import annotations

import copy
import json
import math
import os
import sys
import unicodedata
import warnings
from collections import deque, defaultdict
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple, TypeVar

from wcwidth import wcwidth as _wcwidth, wcswidth as _wcswidth  # type: ignore[import-untyped]

from . import (
    charsets as cs,
    control as ctrl,
    graphics as g,
    modes as mo
)
from .streams import Stream

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from typing import Any, NamedTuple, TextIO

wcswidth: Callable[[str], int] = lru_cache(maxsize=4096)(_wcswidth)
wcwidth: Callable[[str], int] = lru_cache(maxsize=4096)(_wcwidth)

KT = TypeVar("KT")
VT = TypeVar("VT")

class Margins(NamedTuple):
    """A container for screen's scroll margins."""
    top: int
    bottom: int

class Savepoint(NamedTuple):
    """A container for savepoint, created on :data:`~pyte.escape.DECSC`."""
    cursor: Cursor
    g0_charset: str
    g1_charset: str
    charset: int
    origin: bool
    wrap: bool


class Char(NamedTuple):
    """A single styled on-screen character.

    :param str data: unicode character or grapheme cluster.
    :param str fg: foreground colour. Defaults to ``"default"``.
    :param str bg: background colour. Defaults to ``"default"``.
    :param bool bold: flag for rendering the character using bold font.
                      Defaults to ``False``.
    :param bool italics: flag for rendering the character using italic font.
                         Defaults to ``False``.
    :param bool underscore: flag for rendering the character underlined.
                            Defaults to ``False``.
    :param bool strikethrough: flag for rendering the character with a
                               strike-through line. Defaults to ``False``.
    :param bool reverse: flag for swapping foreground and background colours
                         during rendering. Defaults to ``False``.
    :param bool blink: flag for rendering the character blinked. Defaults to
                       ``False``.
    """
    data: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


class Cursor:
    """Screen cursor.

    :param int x: 0-based horizontal cursor position.
    :param int y: 0-based vertical cursor position.
    :param pyte.screens.Char attrs: cursor attributes (see
        :meth:`~pyte.screens.Screen.select_graphic_rendition`
        for details).
    """
    __slots__ = ("x", "y", "attrs", "hidden")

    def __init__(self, x: int, y: int, attrs: Char = Char(" ")) -> None:
        self.x = x
        self.y = y
        self.attrs = attrs
        self.hidden = False


def grapheme_clusters(text: str) -> "Generator[str, None, None]":
    """Yield grapheme clusters from *text*."""
    cluster = ""
    for char in text:
        if not cluster:
            cluster = char
            continue
        if (
            cluster.endswith("\u200d")
            or unicodedata.combining(char)
            or char == "\u200d"
            or 0xFE00 <= ord(char) <= 0xFE0F
            or 0x1F3FB <= ord(char) <= 0x1F3FF
        ):
            cluster += char
        else:
            yield cluster
            cluster = char
    if cluster:
        yield cluster


class StaticDefaultDict(dict[KT, VT]):
    """A :func:`dict` with a static default value.

    Unlike :func:`collections.defaultdict` this implementation does not
    implicitly update the mapping when queried with a missing key.

    >>> d = StaticDefaultDict(42)
    >>> d["foo"]
    42
    >>> d
    {}
    """
    def __init__(self, default: VT) -> None:
        self.default = default

    def __missing__(self, key: KT) -> VT:
        return self.default


_DEFAULT_MODE = {mo.DECAWM, mo.DECTCEM}


class Screen:
    """
    A screen is an in-memory matrix of characters that represents the
    screen display of the terminal. It can be instantiated on its own
    and given explicit commands, or it can be attached to a stream and
    will respond to events.

    .. attribute:: buffer

       A sparse ``lines x columns`` :class:`~pyte.screens.Char` matrix.

    .. attribute:: dirty

       A set of line numbers, which should be re-drawn. The user is responsible
       for clearing this set when changes have been applied.

       >>> screen = Screen(80, 24)
       >>> screen.dirty.clear()
       >>> screen.draw("!")
       >>> list(screen.dirty)
       [0]

       .. versionadded:: 0.7.0

    .. attribute:: cursor

       Reference to the :class:`~pyte.screens.Cursor` object, holding
       cursor position and attributes.

    .. attribute:: margins

       Margins determine which screen lines move during scrolling
       (see :meth:`index` and :meth:`reverse_index`). Characters added
       outside the scrolling region do not make the screen to scroll.

       The value is ``None`` if margins are set to screen boundaries,
       otherwise -- a pair 0-based top and bottom line indices.

    .. attribute:: charset

       Current charset number; can be either ``0`` or ``1`` for `G0`
       and `G1` respectively, note that `G0` is activated by default.

    .. note::

       According to ``ECMA-48`` standard, **lines and columns are
       1-indexed**, so, for instance ``ESC [ 10;10 f`` really means
       -- move cursor to position (9, 9) in the display matrix.

    .. versionchanged:: 0.4.7
    .. warning::

       :data:`~pyte.modes.LNM` is reset by default, to match VT220
       specification. Unfortunately this makes :mod:`pyte` fail
       ``vttest`` for cursor movement.

    .. versionchanged:: 0.4.8
    .. warning::

       If `DECAWM` mode is set than a cursor will be wrapped to the
       **beginning** of the next line, which is the behaviour described
       in ``man console_codes``.

    .. seealso::

       `Standard ECMA-48, Section 6.1.1 \
       <http://ecma-international.org/publications/standards/Ecma-048.htm>`_
       for a description of the presentational component, implemented
       by ``Screen``.
    """
    @property
    def default_char(self) -> Char:
        """An empty character with default foreground and background colors."""
        reverse = mo.DECSCNM in self.mode
        return Char(data=" ", fg="default", bg="default", reverse=reverse)

    def __init__(self, columns: int, lines: int) -> None:
        self.savepoints: list[Savepoint] = []
        self.columns = columns
        self.lines = lines
        self.buffer: dict[int, StaticDefaultDict[int, Char]] = defaultdict(lambda: StaticDefaultDict[int, Char](self.default_char))
        self.dirty: set[int] = set()
        self.reset()
        self.mode = _DEFAULT_MODE.copy()
        self.margins: Margins | None = None

    def __repr__(self) -> str:
        return ("{}({}, {})".format(self.__class__.__name__,
                                       self.columns, self.lines))

    @property
    def display(self) -> list[str]:
        """A :func:`list` of screen lines as unicode strings."""
        def render(line: StaticDefaultDict[int, Char]) -> Generator[str]:
            is_wide_char = False
            for x in range(self.columns):
                if is_wide_char:  # Skip stub
                    is_wide_char = False
                    continue
                char = line[x].data
                char_width = wcswidth(char)
                is_wide_char = char_width == 2
                yield char

        return ["".join(render(self.buffer[y])) for y in range(self.lines)]

    def reset(self) -> None:
        """Reset the terminal to its initial state.

        * Scrolling margins are reset to screen boundaries.
        * Cursor is moved to home location -- ``(0, 0)`` and its
          attributes are set to defaults (see :attr:`default_char`).
        * Screen is cleared -- each character is reset to
          :attr:`default_char`.
        * Tabstops are reset to "every eight columns".
        * All lines are marked as :attr:`dirty`.

        .. note::

           Neither VT220 nor VT102 manuals mention that terminal modes
           and tabstops should be reset as well, thanks to
           :manpage:`xterm` -- we now know that.
        """
        self.dirty.update(range(self.lines))
        self.buffer.clear()
        self.margins = None

        self.mode = _DEFAULT_MODE.copy()

        self.title = ""
        self.icon_name = ""

        self.charset = 0
        self.g0_charset = cs.LAT1_MAP
        self.g1_charset = cs.VT100_MAP

        # From ``man terminfo`` -- "... hardware tabs are initially
        # set every `n` spaces when the terminal is powered up. Since
        # we aim to support VT102 / VT220 and linux -- we use n = 8.
        self.tabstops = set(range(8, self.columns, 8))

        self.cursor = Cursor(0, 0)
        self.cursor_position()

        self.saved_columns: int | None = None

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Resize the screen to the given size.

        If the requested screen size has more lines than the existing
        screen, lines will be added at the bottom. If the requested
        size has less lines than the existing screen lines will be
        clipped at the top of the screen. Similarly, if the existing
        screen has less columns than the requested screen, columns will
        be added at the right, and if it has more -- columns will be
        clipped at the right.

        :param int lines: number of lines in the new screen.
        :param int columns: number of columns in the new screen.

        .. versionchanged:: 0.7.0

           If the requested screen size is identical to the current screen
           size, the method does nothing.
        """
        lines = lines or self.lines
        columns = columns or self.columns

        if lines == self.lines and columns == self.columns:
            return  # No changes.

        self.dirty.update(range(lines))

        if lines < self.lines:
            self.save_cursor()
            self.cursor_position(0, 0)
            self.delete_lines(self.lines - lines)  # Drop from the top.
            self.restore_cursor()

        if columns < self.columns:
            for line in self.buffer.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)

        self.lines, self.columns = lines, columns
        self.set_margins()

    def set_margins(self, top: int | None = None, bottom: int | None = None) -> None:
        """Select top and bottom margins for the scrolling region.

        :param int top: the smallest line number that is scrolled.
        :param int bottom: the biggest line number that is scrolled.
        """
        # XXX 0 corresponds to the CSI with no parameters.
        if (top is None or top == 0) and bottom is None:
            self.margins = None
            return

        margins = self.margins or Margins(0, self.lines - 1)

        # Arguments are 1-based, while :attr:`margins` are zero
        # based -- so we have to decrement them by one. We also
        # make sure that both of them is bounded by [0, lines - 1].
        if top is None:
            top = margins.top
        else:
            top = max(0, min(top - 1, self.lines - 1))
        if bottom is None:
            bottom = margins.bottom
        else:
            bottom = max(0, min(bottom - 1, self.lines - 1))

        # Even though VT102 and VT220 require DECSTBM to ignore
        # regions of width less than 2, some programs (like aptitude
        # for example) rely on it. Practicality beats purity.
        if bottom - top >= 1:
            self.margins = Margins(top, bottom)

            # The cursor moves to the home position when the top and
            # bottom margins of the scrolling region (DECSTBM) changes.
            self.cursor_position()

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        """Set (enable) a given list of modes.

        :param list modes: modes to set, where each mode is a constant
                           from :mod:`pyte.modes`.
        """
        mode_list = list(modes)
        # Private mode codes are shifted, to be distinguished from non
        # private ones.
        if kwargs.get("private"):
            mode_list = [mode << 5 for mode in modes]
            if mo.DECSCNM in mode_list:
                self.dirty.update(range(self.lines))

        self.mode.update(mode_list)

        # When DECOLM mode is set, the screen is erased and the cursor
        # moves to the home position.
        if mo.DECCOLM in mode_list:
            self.saved_columns = self.columns
            self.resize(columns=132)
            self.erase_in_display(2)
            self.cursor_position()

        # According to VT520 manual, DECOM should also home the cursor.
        if mo.DECOM in mode_list:
            self.cursor_position()

        # Mark all displayed characters as reverse.
        if mo.DECSCNM in mode_list:
            for line in self.buffer.values():
                line.default = self.default_char
                for x in line:
                    line[x] = line[x]._replace(reverse=True)

            self.select_graphic_rendition(7)  # +reverse.

        # Make the cursor visible.
        if mo.DECTCEM in mode_list:
            self.cursor.hidden = False

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        """Reset (disable) a given list of modes.

        :param list modes: modes to reset -- hopefully, each mode is a
                           constant from :mod:`pyte.modes`.
        """
        mode_list = list(modes)
        # Private mode codes are shifted, to be distinguished from non
        # private ones.
        if kwargs.get("private"):
            mode_list = [mode << 5 for mode in modes]
            if mo.DECSCNM in mode_list:
                self.dirty.update(range(self.lines))

        self.mode.difference_update(mode_list)

        # Lines below follow the logic in :meth:`set_mode`.
        if mo.DECCOLM in mode_list:
            if self.columns == 132 and self.saved_columns is not None:
                self.resize(columns=self.saved_columns)
                self.saved_columns = None
            self.erase_in_display(2)
            self.cursor_position()

        if mo.DECOM in mode_list:
            self.cursor_position()

        if mo.DECSCNM in mode_list:
            for line in self.buffer.values():
                line.default = self.default_char
                for x in line:
                    line[x] = line[x]._replace(reverse=False)

            self.select_graphic_rendition(27)  # -reverse.

        # Hide the cursor.
        if mo.DECTCEM in mode_list:
            self.cursor.hidden = True

    def define_charset(self, code: str, mode: str) -> None:
        """Define ``G0`` or ``G1`` charset.

        :param str code: character set code, should be a character
                         from ``"B0UK"``, otherwise ignored.
        :param str mode: if ``"("`` ``G0`` charset is defined, if
                         ``")"`` -- we operate on ``G1``.

        .. warning:: User-defined charsets are currently not supported.
        """
        if code in cs.MAPS:
            if mode == "(":
                self.g0_charset = cs.MAPS[code]
            elif mode == ")":
                self.g1_charset = cs.MAPS[code]

    def shift_in(self) -> None:
        """Select ``G0`` character set."""
        self.charset = 0

    def shift_out(self) -> None:
        """Select ``G1`` character set."""
        self.charset = 1

    def draw(self, data: str) -> None:
        """Display decoded characters at the current cursor position and
        advances the cursor if :data:`~pyte.modes.DECAWM` is set.

        :param str data: text to display.

        .. versionchanged:: 0.5.0

           Character width is taken into account. Specifically, zero-width
           and unprintable characters do not affect screen state. Full-width
           characters are rendered into two consecutive character containers.
        """
        data = data.translate(
            self.g1_charset if self.charset else self.g0_charset)

        for char in grapheme_clusters(data):
            char_width = wcswidth(char)

            # If this was the last column in a line and auto wrap mode is
            # enabled, move the cursor to the beginning of the next line,
            # otherwise replace characters already displayed with newly
            # entered.
            if self.cursor.x == self.columns:
                if mo.DECAWM in self.mode:
                    self.dirty.add(self.cursor.y)
                    self.carriage_return()
                    self.linefeed()
                elif char_width > 0:
                    self.cursor.x -= char_width

            # If Insert mode is set, new characters move old characters to
            # the right, otherwise terminal is in Replace mode and new
            # characters replace old characters at cursor position.
            if mo.IRM in self.mode and char_width > 0:
                self.insert_characters(char_width)

            line = self.buffer[self.cursor.y]
            if char_width == 1:
                line[self.cursor.x] = self.cursor.attrs._replace(data=char)
            elif char_width == 2:
                # A two-cell character has a stub slot after it.
                line[self.cursor.x] = self.cursor.attrs._replace(data=char)
                if self.cursor.x + 1 < self.columns:
                    line[self.cursor.x + 1] = self.cursor.attrs \
                        ._replace(data="")
            elif char_width == 0 and all(unicodedata.combining(c) for c in char):
                # A zero-cell character is combined with the previous
                # character either on this or preceding line.
                if self.cursor.x:
                    last = line[self.cursor.x - 1]
                    normalized = unicodedata.normalize("NFC", last.data + char)
                    line[self.cursor.x - 1] = last._replace(data=normalized)
                elif self.cursor.y:
                    last = self.buffer[self.cursor.y - 1][self.columns - 1]
                    normalized = unicodedata.normalize("NFC", last.data + char)
                    self.buffer[self.cursor.y - 1][self.columns - 1] = \
                        last._replace(data=normalized)
            else:
                break  # Unprintable character or doesn't advance the cursor.

            # .. note:: We can't use :meth:`cursor_forward()`, because that
            #           way, we'll never know when to linefeed.
            if char_width > 0:
                self.cursor.x = min(self.cursor.x + char_width, self.columns)

        self.dirty.add(self.cursor.y)

    def set_title(self, param: str) -> None:
        """Set terminal title.

        .. note:: This is an XTerm extension supported by the Linux terminal.
        """
        self.title = param

    def set_icon_name(self, param: str) -> None:
        """Set icon name.

        .. note:: This is an XTerm extension supported by the Linux terminal.
        """
        self.icon_name = param

    def carriage_return(self) -> None:
        """Move the cursor to the beginning of the current line."""
        self.cursor.x = 0

    def index(self) -> None:
        """Move the cursor down one line in the same column. If the
        cursor is at the last line, create a new line at the bottom.
        """
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom:
            # TODO: mark only the lines within margins?
            self.dirty.update(range(self.lines))
            for y in range(top, bottom):
                self.buffer[y] = self.buffer[y + 1]
            self.buffer.pop(bottom, None)
        else:
            self.cursor_down()

    def reverse_index(self) -> None:
        """Move the cursor up one line in the same column. If the cursor
        is at the first line, create a new line at the top.
        """
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == top:
            # TODO: mark only the lines within margins?
            self.dirty.update(range(self.lines))
            for y in range(bottom, top, -1):
                self.buffer[y] = self.buffer[y - 1]
            self.buffer.pop(top, None)
        else:
            self.cursor_up()

    def linefeed(self) -> None:
        """Perform an index and, if :data:`~pyte.modes.LNM` is set, a
        carriage return.
        """
        self.index()

        if mo.LNM in self.mode:
            self.carriage_return()

    def tab(self) -> None:
        """Move to the next tab space, or the end of the screen if there
        aren't anymore left.
        """
        for stop in sorted(self.tabstops):
            if self.cursor.x < stop:
                column = stop
                break
        else:
            column = self.columns - 1

        self.cursor.x = column

    def backspace(self) -> None:
        """Move cursor to the left one or keep it in its position if
        it's at the beginning of the line already.
        """
        self.cursor_back()

    def save_cursor(self) -> None:
        """Push the current cursor position onto the stack."""
        self.savepoints.append(Savepoint(copy.copy(self.cursor),
                                         self.g0_charset,
                                         self.g1_charset,
                                         self.charset,
                                         mo.DECOM in self.mode,
                                         mo.DECAWM in self.mode))

    def restore_cursor(self) -> None:
        """Set the current cursor position to whatever cursor is on top
        of the stack.
        """
        if self.savepoints:
            savepoint = self.savepoints.pop()

            self.g0_charset = savepoint.g0_charset
            self.g1_charset = savepoint.g1_charset
            self.charset = savepoint.charset

            if savepoint.origin:
                self.set_mode(mo.DECOM)
            if savepoint.wrap:
                self.set_mode(mo.DECAWM)

            self.cursor = savepoint.cursor
            self.ensure_hbounds()
            self.ensure_vbounds(use_margins=True)
        else:
            # If nothing was saved, the cursor moves to home position;
            # origin mode is reset. :todo: DECAWM?
            self.reset_mode(mo.DECOM)
            self.cursor_position()

    def insert_lines(self, count: int | None = None) -> None:
        """Insert the indicated # of lines at line with cursor. Lines
        displayed **at** and below the cursor move down. Lines moved
        past the bottom margin are lost.

        :param count: number of lines to insert.
        """
        count = count or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)

        # If cursor is outside scrolling margins it -- do nothin'.
        if top <= self.cursor.y <= bottom:
            self.dirty.update(range(self.cursor.y, self.lines))
            for y in range(bottom, self.cursor.y - 1, -1):
                if y + count <= bottom and y in self.buffer:
                    self.buffer[y + count] = self.buffer[y]
                self.buffer.pop(y, None)

            self.carriage_return()

    def delete_lines(self, count: int | None = None) -> None:
        """Delete the indicated # of lines, starting at line with
        cursor. As lines are deleted, lines displayed below cursor
        move up. Lines added to bottom of screen have spaces with same
        character attributes as last line moved up.

        :param int count: number of lines to delete.
        """
        count = count or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)

        # If cursor is outside scrolling margins -- do nothin'.
        if top <= self.cursor.y <= bottom:
            self.dirty.update(range(self.cursor.y, self.lines))
            for y in range(self.cursor.y, bottom + 1):
                if y + count <= bottom:
                    if y + count in self.buffer:
                        self.buffer[y] = self.buffer.pop(y + count)
                else:
                    self.buffer.pop(y, None)

            self.carriage_return()

    def insert_characters(self, count: int | None = None) -> None:
        """Insert the indicated # of blank characters at the cursor
        position. The cursor does not move and remains at the beginning
        of the inserted blank characters. Data on the line is shifted
        forward.

        :param int count: number of characters to insert.
        """
        self.dirty.add(self.cursor.y)

        count = count or 1
        line = self.buffer[self.cursor.y]
        for x in range(self.columns, self.cursor.x - 1, -1):
            if x + count <= self.columns:
                line[x + count] = line[x]
            line.pop(x, None)

    def delete_characters(self, count: int | None = None) -> None:
        """Delete the indicated # of characters, starting with the
        character at cursor position. When a character is deleted, all
        characters to the right of cursor move left. Character attributes
        move with the characters.

        :param int count: number of characters to delete.
        """
        self.dirty.add(self.cursor.y)
        count = count or 1

        line = self.buffer[self.cursor.y]
        for x in range(self.cursor.x, self.columns):
            if x + count <= self.columns:
                line[x] = line.pop(x + count, self.default_char)
            else:
                line.pop(x, None)

    def erase_characters(self, count: int | None = None) -> None:
        """Erase the indicated # of characters, starting with the
        character at cursor position. Character attributes are set
        cursor attributes. The cursor remains in the same position.

        :param int count: number of characters to erase.

        .. note::

           Using cursor attributes for character attributes may seem
           illogical, but if recall that a terminal emulator emulates
           a type writer, it starts to make sense. The only way a type
           writer could erase a character is by typing over it.
        """
        self.dirty.add(self.cursor.y)
        count = count or 1

        line = self.buffer[self.cursor.y]
        for x in range(self.cursor.x,
                       min(self.cursor.x + count, self.columns)):
            line[x] = self.cursor.attrs

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        """Erase a line in a specific way.

        Character attributes are set to cursor attributes.

        :param int how: defines the way the line should be erased in:

            * ``0`` -- Erases from cursor to end of line, including cursor
              position.
            * ``1`` -- Erases from beginning of line to cursor,
              including cursor position.
            * ``2`` -- Erases complete line.
        :param bool private: when ``True`` only characters marked as
                             erasable are affected **not implemented**.
        """
        self.dirty.add(self.cursor.y)
        if how == 0:
            interval = range(self.cursor.x, self.columns)
        elif how == 1:
            interval = range(self.cursor.x + 1)
        elif how == 2:
            interval = range(self.columns)

        line = self.buffer[self.cursor.y]
        for x in interval:
            line[x] = self.cursor.attrs

    def erase_in_display(self, how: int= 0, *args: Any, **kwargs: Any) -> None:
        """Erases display in a specific way.

        Character attributes are set to cursor attributes.

        :param int how: defines the way the line should be erased in:

            * ``0`` -- Erases from cursor to end of screen, including
              cursor position.
            * ``1`` -- Erases from beginning of screen to cursor,
              including cursor position.
            * ``2`` and ``3`` -- Erases complete display. All lines
              are erased and changed to single-width. Cursor does not
              move.
        :param bool private: when ``True`` only characters marked as
                             erasable are affected **not implemented**.

        .. versionchanged:: 0.8.1

           The method accepts any number of positional arguments as some
           ``clear`` implementations include a ``;`` after the first
           parameter causing the stream to assume a ``0`` second parameter.
        """
        if how == 0:
            interval = range(self.cursor.y + 1, self.lines)
        elif how == 1:
            interval = range(self.cursor.y)
        elif how == 2 or how == 3:
            interval = range(self.lines)

        self.dirty.update(interval)
        for y in interval:
            line = self.buffer[y]
            for x in line:
                line[x] = self.cursor.attrs

        if how == 0 or how == 1:
            self.erase_in_line(how)

    def set_tab_stop(self) -> None:
        """Set a horizontal tab stop at cursor position."""
        self.tabstops.add(self.cursor.x)

    def clear_tab_stop(self, how: int = 0) -> None:
        """Clear a horizontal tab stop.

        :param int how: defines a way the tab stop should be cleared:

            * ``0`` or nothing -- Clears a horizontal tab stop at cursor
              position.
            * ``3`` -- Clears all horizontal tab stops.
        """
        if how == 0:
            # Clears a horizontal tab stop at cursor position, if it's
            # present, or silently fails if otherwise.
            self.tabstops.discard(self.cursor.x)
        elif how == 3:
            self.tabstops = set()  # Clears all horizontal tab stops.

    def ensure_hbounds(self) -> None:
        """Ensure the cursor is within horizontal screen bounds."""
        self.cursor.x = min(max(0, self.cursor.x), self.columns - 1)

    def ensure_vbounds(self, use_margins: bool | None = None) -> None:
        """Ensure the cursor is within vertical screen bounds.

        :param bool use_margins: when ``True`` or when
                                 :data:`~pyte.modes.DECOM` is set,
                                 cursor is bounded by top and and bottom
                                 margins, instead of ``[0; lines - 1]``.
        """
        if (use_margins or mo.DECOM in self.mode) and self.margins is not None:
            top, bottom = self.margins
        else:
            top, bottom = 0, self.lines - 1

        self.cursor.y = min(max(top, self.cursor.y), bottom)

    def cursor_up(self, count: int | None = None) -> None:
        """Move cursor up the indicated # of lines in same column.
        Cursor stops at top margin.

        :param int count: number of lines to skip.
        """
        top, _bottom = self.margins or Margins(0, self.lines - 1)
        self.cursor.y = max(self.cursor.y - (count or 1), top)

    def cursor_up1(self, count: int | None = None) -> None:
        """Move cursor up the indicated # of lines to column 1. Cursor
        stops at bottom margin.

        :param int count: number of lines to skip.
        """
        self.cursor_up(count)
        self.carriage_return()

    def cursor_down(self, count: int | None = None) -> None:
        """Move cursor down the indicated # of lines in same column.
        Cursor stops at bottom margin.

        :param int count: number of lines to skip.
        """
        _top, bottom = self.margins or Margins(0, self.lines - 1)
        self.cursor.y = min(self.cursor.y + (count or 1), bottom)

    def cursor_down1(self, count: int | None = None) -> None:
        """Move cursor down the indicated # of lines to column 1.
        Cursor stops at bottom margin.

        :param int count: number of lines to skip.
        """
        self.cursor_down(count)
        self.carriage_return()

    def cursor_back(self, count: int | None = None) -> None:
        """Move cursor left the indicated # of columns. Cursor stops
        at left margin.

        :param int count: number of columns to skip.
        """
        # Handle the case when we've just drawn in the last column
        # and would wrap the line on the next :meth:`draw()` call.
        if self.cursor.x == self.columns:
            self.cursor.x -= 1

        self.cursor.x -= count or 1
        self.ensure_hbounds()

    def cursor_forward(self, count: int | None = None) -> None:
        """Move cursor right the indicated # of columns. Cursor stops
        at right margin.

        :param int count: number of columns to skip.
        """
        self.cursor.x += count or 1
        self.ensure_hbounds()

    def cursor_position(self, line: int | None = None, column: int | None = None) -> None:
        """Set the cursor to a specific `line` and `column`.

        Cursor is allowed to move out of the scrolling region only when
        :data:`~pyte.modes.DECOM` is reset, otherwise -- the position
        doesn't change.

        :param int line: line number to move the cursor to.
        :param int column: column number to move the cursor to.
        """
        column = (column or 1) - 1
        line = (line or 1) - 1

        # If origin mode (DECOM) is set, line number are relative to
        # the top scrolling margin.
        if self.margins is not None and mo.DECOM in self.mode:
            line += self.margins.top

            # Cursor is not allowed to move out of the scrolling region.
            if not self.margins.top <= line <= self.margins.bottom:
                return

        self.cursor.x = column
        self.cursor.y = line
        self.ensure_hbounds()
        self.ensure_vbounds()

    def cursor_to_column(self, column: int | None = None) -> None:
        """Move cursor to a specific column in the current line.

        :param int column: column number to move the cursor to.
        """
        self.cursor.x = (column or 1) - 1
        self.ensure_hbounds()

    def cursor_to_line(self, line: int | None = None) -> None:
        """Move cursor to a specific line in the current column.

        :param int line: line number to move the cursor to.
        """
        self.cursor.y = (line or 1) - 1

        # If origin mode (DECOM) is set, line number are relative to
        # the top scrolling margin.
        if mo.DECOM in self.mode:
            assert self.margins is not None
            self.cursor.y += self.margins.top

            # FIXME: should we also restrict the cursor to the scrolling
            # region?

        self.ensure_vbounds()

    def bell(self, *args: Any) -> None:
        """Bell stub -- the actual implementation should probably be
        provided by the end-user.
        """

    def alignment_display(self) -> None:
        """Fills screen with uppercase E's for screen focus and alignment."""
        self.dirty.update(range(self.lines))
        for y in range(self.lines):
            for x in range(self.columns):
                self.buffer[y][x] = self.buffer[y][x]._replace(data="E")

    def select_graphic_rendition(self, *attrs: int) -> None:
        """Set display attributes.

        :param list attrs: a list of display attributes to set.
        """
        replace = {}

        # Fast path for resetting all attributes.
        if not attrs or attrs == (0, ):
            self.cursor.attrs = self.default_char
            return

        attrs_list = list(reversed(attrs))

        while attrs_list:
            attr = attrs_list.pop()
            if attr == 0:
                # Reset all attributes.
                replace.update(self.default_char._asdict())
            elif attr in g.FG_ANSI:
                replace["fg"] = g.FG_ANSI[attr]
            elif attr in g.BG:
                replace["bg"] = g.BG_ANSI[attr]
            elif attr in g.TEXT:
                attr_str = g.TEXT[attr]
                replace[attr_str[1:]] = attr_str.startswith("+")
            elif attr in g.FG_AIXTERM:
                replace.update(fg=g.FG_AIXTERM[attr])
            elif attr in g.BG_AIXTERM:
                replace.update(bg=g.BG_AIXTERM[attr])
            elif attr in (g.FG_256, g.BG_256):
                key = "fg" if attr == g.FG_256 else "bg"
                try:
                    n = attrs_list.pop()
                    if n == 5:    # 256.
                        m = attrs_list.pop()
                        replace[key] = g.FG_BG_256[m]
                    elif n == 2:  # 24bit.
                        # This is somewhat non-standard but is nonetheless
                        # supported in quite a few terminals. See discussion
                        # here https://gist.github.com/XVilka/8346728.
                        replace[key] = "{:02x}{:02x}{:02x}".format(
                            attrs_list.pop(), attrs_list.pop(), attrs_list.pop())
                except IndexError:
                    pass

        self.cursor.attrs = self.cursor.attrs._replace(**replace)

    def report_device_attributes(self, mode: int = 0, **kwargs: bool) -> None:
        """Report terminal identity.

        .. versionadded:: 0.5.0

        .. versionchanged:: 0.7.0

           If ``private`` keyword argument is set, the method does nothing.
           This behaviour is consistent with VT220 manual.
        """
        # We only implement "primary" DA which is the only DA request
        # VT102 understood, see ``VT102ID`` in ``linux/drivers/tty/vt.c``.
        if mode == 0 and not kwargs.get("private"):
            self.write_process_input(ctrl.CSI + "?6c")

    def report_device_status(self, mode: int) -> None:
        """Report terminal status or cursor position.

        :param int mode: if 5 -- terminal status, 6 -- cursor position,
                         otherwise a noop.

        .. versionadded:: 0.5.0
        """
        if mode == 5:    # Request for terminal status.
            self.write_process_input(ctrl.CSI + "0n")
        elif mode == 6:  # Request for cursor position.
            x = self.cursor.x + 1
            y = self.cursor.y + 1

            # "Origin mode (DECOM) selects line numbering."
            if mo.DECOM in self.mode:
                assert self.margins is not None
                y -= self.margins.top
            self.write_process_input(ctrl.CSI + f"{y};{x}R")

    def write_process_input(self, data: str) -> None:
        """Write data to the process running inside the terminal.

        By default is a noop.

        :param str data: text to write to the process ``stdin``.

        .. versionadded:: 0.5.0
        """

    def debug(self, *args: Any, **kwargs: Any) -> None:
        """Endpoint for unrecognized escape sequences.

        By default is a noop.
        """


class DiffScreen(Screen):
    """
    A screen subclass, which maintains a set of dirty lines in its
    :attr:`dirty` attribute. The end user is responsible for emptying
    a set, when a diff is applied.

    .. deprecated:: 0.7.0

       The functionality contained in this class has been merged into
       :class:`~pyte.screens.Screen` and will be removed in 0.8.0.
       Please update your code accordingly.
    """
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "The functionality of ``DiffScreen` has been merged into "
            "``Screen`` and will be removed in 0.8.0. Please update "
            "your code accordingly.", DeprecationWarning)

        super().__init__(*args, **kwargs)


class History(NamedTuple):
    top: deque[StaticDefaultDict[int, Char]]
    bottom: deque[StaticDefaultDict[int, Char]]
    ratio: float
    size: int
    position: int


class HistoryScreen(Screen):
    """A :class:`~pyte.screens.Screen` subclass, which keeps track
    of screen history and allows pagination. This is not linux-specific,
    but still useful; see page 462 of VT520 User's Manual.

    :param int history: total number of history lines to keep; is split
                        between top and bottom queues.
    :param int ratio: defines how much lines to scroll on :meth:`next_page`
                      and :meth:`prev_page` calls.

    .. attribute:: history

       A pair of history queues for top and bottom margins accordingly;
       here's the overall screen structure::

            [ 1: .......]
            [ 2: .......]  <- top history
            [ 3: .......]
            ------------
            [ 4: .......]  s
            [ 5: .......]  c
            [ 6: .......]  r
            [ 7: .......]  e
            [ 8: .......]  e
            [ 9: .......]  n
            ------------
            [10: .......]
            [11: .......]  <- bottom history
            [12: .......]

    .. note::

       Don't forget to update :class:`~pyte.streams.Stream` class with
       appropriate escape sequences -- you can use any, since pagination
       protocol is not standardized, for example::

           Stream.escape["N"] = "next_page"
           Stream.escape["P"] = "prev_page"
    """
    _wrapped = set(Stream.events)
    _wrapped.update(["next_page", "prev_page"])

    def __init__(self, columns: int, lines: int, history: int = 100, ratio: float = .5) -> None:
        self.history = History(deque(maxlen=history),
                               deque(maxlen=history),
                               float(ratio),
                               history,
                               history)

        super().__init__(columns, lines)

    def _make_wrapper(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            self.before_event(event)
            result = handler(*args, **kwargs)
            self.after_event(event)
            return result
        return inner

    def __getattribute__(self, attr: str) -> Callable[..., Any]:
        value = super().__getattribute__(attr)
        if attr in HistoryScreen._wrapped:
            return HistoryScreen._make_wrapper(self, attr, value)
        else:
            return value  # type: ignore[no-any-return]

    def before_event(self, event: str) -> None:
        """Ensure a screen is at the bottom of the history buffer.

        :param str event: event name, for example ``"linefeed"``.
        """
        if event not in ["prev_page", "next_page"]:
            while self.history.position < self.history.size:
                self.next_page()

    def after_event(self, event: str) -> None:
        """Ensure all lines on a screen have proper width (:attr:`columns`).

        Extra characters are truncated, missing characters are filled
        with whitespace.

        :param str event: event name, for example ``"linefeed"``.
        """
        if event in ["prev_page", "next_page"]:
            for line in self.buffer.values():
                for x in line:
                    if x > self.columns:
                        line.pop(x)

        # If we're at the bottom of the history buffer and `DECTCEM`
        # mode is set -- show the cursor.
        self.cursor.hidden = not (
            self.history.position == self.history.size and
            mo.DECTCEM in self.mode
        )

    def _reset_history(self) -> None:
        self.history.top.clear()
        self.history.bottom.clear()
        self.history = self.history._replace(position=self.history.size)

    def reset(self) -> None:
        """Overloaded to reset screen history state: history position
        is reset to bottom of both queues;  queues themselves are
        emptied.
        """
        super().reset()
        self._reset_history()

    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
        """Overloaded to reset history state."""
        super().erase_in_display(how, *args, **kwargs)

        if how == 3:
            self._reset_history()

    def index(self) -> None:
        """Overloaded to update top history with the removed lines."""
        top, bottom = self.margins or Margins(0, self.lines - 1)

        if self.cursor.y == bottom:
            self.history.top.append(self.buffer[top])

        super().index()

    def reverse_index(self) -> None:
        """Overloaded to update bottom history with the removed lines."""
        top, bottom = self.margins or Margins(0, self.lines - 1)

        if self.cursor.y == top:
            self.history.bottom.append(self.buffer[bottom])

        super().reverse_index()

    def prev_page(self) -> None:
        """Move the screen page up through the history buffer. Page
        size is defined by ``history.ratio``, so for instance
        ``ratio = .5`` means that half the screen is restored from
        history on page switch.
        """
        if self.history.position > self.lines and self.history.top:
            mid = min(len(self.history.top),
                      int(math.ceil(self.lines * self.history.ratio)))

            self.history.bottom.extendleft(
                self.buffer[y]
                for y in range(self.lines - 1, self.lines - mid - 1, -1))
            self.history = self.history \
                ._replace(position=self.history.position - mid)

            for y in range(self.lines - 1, mid - 1, -1):
                self.buffer[y] = self.buffer[y - mid]
            for y in range(mid - 1, -1, -1):
                self.buffer[y] = self.history.top.pop()

            self.dirty = set(range(self.lines))

    def next_page(self) -> None:
        """Move the screen page down through the history buffer."""
        if self.history.position < self.history.size and self.history.bottom:
            mid = min(len(self.history.bottom),
                      int(math.ceil(self.lines * self.history.ratio)))

            self.history.top.extend(self.buffer[y] for y in range(mid))
            self.history = self.history \
                ._replace(position=self.history.position + mid)

            for y in range(self.lines - mid):
                self.buffer[y] = self.buffer[y + mid]
            for y in range(self.lines - mid, self.lines):
                self.buffer[y] = self.history.bottom.popleft()

            self.dirty = set(range(self.lines))


class DebugEvent(NamedTuple):
    """Event dispatched to :class:`~pyte.screens.DebugScreen`.

    .. warning::

       This is developer API with no backward compatibility guarantees.
       Use at your own risk!
    """
    name: str
    args: Any
    kwargs: Any

    @staticmethod
    def from_string(line: str) -> DebugEvent:
        return DebugEvent(*json.loads(line))

    def __str__(self) -> str:
        return json.dumps(self)

    def __call__(self, screen: Screen) -> Any:
        """Execute this event on a given ``screen``."""
        return getattr(screen, self.name)(*self.args, **self.kwargs)


class DebugScreen:
    r"""A screen which dumps a subset of the received events to a file.

    >>> import io
    >>> with io.StringIO() as buf:
    ...     stream = Stream(DebugScreen(to=buf))
    ...     stream.feed("\x1b[1;24r\x1b[4l\x1b[24;1H\x1b[0;10m")
    ...     print(buf.getvalue())
    ...
    ... # doctest: +NORMALIZE_WHITESPACE
    ["set_margins", [1, 24], {}]
    ["reset_mode", [4], {}]
    ["cursor_position", [24, 1], {}]
    ["select_graphic_rendition", [0, 10], {}]

    :param file to: a file-like object to write debug information to.
    :param list only: a list of events you want to debug (empty by
                      default, which means -- debug all events).

    .. warning::

       This is developer API with no backward compatibility guarantees.
       Use at your own risk!
    """
    def __init__(self, to: TextIO = sys.stderr, only: Sequence[str] = ()) -> None:
        self.to = to
        self.only = only

    def only_wrapper(self, attr: str) -> Callable[..., None]:
        def wrapper(*args: Any, **kwargs: Any) -> None:
            self.to.write(str(DebugEvent(attr, args, kwargs)))
            self.to.write(str(os.linesep))

        return wrapper

    def __getattribute__(self, attr: str) -> Callable[..., None]:
        if attr not in Stream.events:
            return super().__getattribute__(attr)  # type: ignore[no-any-return]
        elif not self.only or attr in self.only:
            return self.only_wrapper(attr)
        else:
            return lambda *args, **kwargs: None
