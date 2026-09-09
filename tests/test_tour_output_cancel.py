import unittest

from bot import _tour_v2_output_keyboard, tour_output_keyboard


class TourOutputCancelTests(unittest.TestCase):
    def test_v2_output_menu_has_cancel_and_start_new(self):
        buttons=[button for row in _tour_v2_output_keyboard().inline_keyboard for button in row]
        self.assertIn(('❌ Cancel & Start New','cancel'),[(x.text,x.callback_data) for x in buttons])

    def test_classic_output_menu_has_cancel_and_start_new(self):
        buttons=[button for row in tour_output_keyboard().inline_keyboard for button in row]
        self.assertIn(('❌ Cancel & Start New','cancel'),[(x.text,x.callback_data) for x in buttons])

    def test_pdf_mode_is_selected_directly_from_output_menu(self):
        buttons=[button for row in _tour_v2_output_keyboard().inline_keyboard for button in row]
        callbacks={x.callback_data for x in buttons}
        self.assertIn('tour_output:pdf:basic:quotation',callbacks)
        self.assertIn('tour_output:pdf:detailed:voucher',callbacks)


if __name__ == '__main__':
    unittest.main()
