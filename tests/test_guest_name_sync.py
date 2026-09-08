import unittest

from editor import apply_edit


class GuestNameSyncTests(unittest.TestCase):
    def test_package_name_updates_top_and_existing_greeting(self):
        source={
            'client_name':'Mr. Old Guest',
            'greeting':'Dear Mr. Old Guest,\n\nGreetings from MyTourBazar!',
            'days':[],
        }
        updated,_=apply_edit('package',source,'change guest name to Mr. New Guest')
        self.assertEqual(updated['client_name'],'Mr. New Guest')
        self.assertIn('Dear Mr. New Guest,',updated['greeting'])
        self.assertNotIn('Mr. Old Guest',updated['greeting'])


if __name__=='__main__':
    unittest.main()
