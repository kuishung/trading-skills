"""The Options Screener's engine (OPTIONS_SCREENER_DESIGN.md §5-§6).

Pure screening over numpy arrays - no HTTP, no DB writes. The routes call the four
functions of ``engine`` (``from app.services.screener import engine``):

* ``engine.screens()``                        the GO TO list, grouped by family
* ``engine.spec(key)``                        one screen: meta, default filters, Add-a-Filter fields, views
* ``engine.run(key, payload, page, per_page)`` the result table (JSON-able dict)
* ``engine.csv(key, payload, limit)``          the same rows as CSV text

``frame.current()`` is the market the engine screens: every kept contract and underlying
of the latest finished market pass, loaded from the screener DB into numpy arrays and
reloaded in the background when a newer pass finishes.

Nothing is re-exported here on purpose: ``screens`` is both a submodule (the 33 screen
definitions) and an engine function, and a package-level alias would shadow the module.
"""
