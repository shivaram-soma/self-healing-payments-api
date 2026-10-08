import os
import sys

# Let the tests import main.py and store.py from the app folder.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
