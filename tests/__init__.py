import os.path
import sys
if sys.version_info >= (2, 7):
    import unittest
else:
    import unittest2 as unittest

def get_tests():
    start_dir = os.path.dirname(__file__)
    return unittest.TestLoader().discover(start_dir, pattern="*.py")
