"""The live engine: the same operations on a document open in Word or LibreOffice.

Nothing here is imported by the file engine, and the app bindings (``uno``, ``pywin32``)
are imported only inside their adapter, so the core stays standard library only.
"""
