::

                       _
                      | |
         _ __   _   _ | |_  ___
        | '_ \ | | | || __|/ _ \
        | |_) || |_| || |_|  __/
        | .__/  \__, | \__|\___|
        | |      __/ |
        |_|     |___/      0.8.3.dev


What is ``pyte``?
-----------------

It's an in memory VTXXX-compatible terminal emulator.
*XXX* stands for a series of video terminals, developed by
`DEC <http://en.wikipedia.org/wiki/Digital_Equipment_Corporation>`_ between
1970 and 1995. The first, and probably the most famous one, was VT100
terminal, which is now a de-facto standard for all virtual terminal
emulators. ``pyte`` follows the suit.

So, why would one need a terminal emulator library?

* To screen scrape terminal apps, for example ``htop`` or ``aptitude``.
* To write cross platform terminal emulators; either with a graphical
  (`xterm <http://invisible-island.net/xterm/>`_,
  `rxvt <http://rxvt.net/>`_) or a web interface, like
  `AjaxTerm <http://antony.lesuisse.org/software/ajaxterm/>`_.
* To have fun, hacking on the ancient, poorly documented technologies.

**Note**: ``pyte`` started as a fork of `vt102 <http://github.com/samfoo/vt102>`_,
which is an incomplete pure Python implementation of VT100 terminal.


Installation
------------

If you have `pip <https://pip.pypa.io/en/stable>`_ you can do the usual::

    pip install pyte

Otherwise, download the source from `GitHub <https://github.com/selectel/pyte>`_
and run::

    python setup.py install

Similar projects
----------------

``pyte`` is not alone in the weird world of terminal emulator libraries,
here's a few other options worth checking out:
`Termemulator <http://sourceforge.net/projects/termemulator/>`_,
`pyqonsole <http://hg.logilab.org/pyqonsole/>`_,
`webtty <http://code.google.com/p/webtty/>`_,
`AjaxTerm <http://antony.lesuisse.org/software/ajaxterm/>`_ and of course
`vt102 <http://github.com/samfoo/vt102>`_.
