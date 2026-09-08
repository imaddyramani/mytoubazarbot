import unittest
from unittest.mock import patch
from identity_guard import best_source_name,reconcile_people
from bus_ticket import extract_bus_ticket
from flight_extractor import extract_flight_ticket
from hotel_voucher import extract_hotel_voucher


class IdentityGuardTests(unittest.TestCase):
    def test_full_source_name_beats_truncated_ai_name(self):
        source='Passenger Name: Mr. Komalkant Kumar Sahu Adult Seat 12A'
        self.assertEqual(best_source_name('Komalkant Sahu','Komalkant Kumar Sahu',source),'Komalkant Kumar Sahu')

    def test_missing_passenger_rows_are_restored(self):
        ai=[{'name':'Ajay Panjwani','seat':'12A','type':'Adult'}]
        local=[{'name':'Ajay Kumar Panjwani','seat':'12A','type':'Adult'},
               {'name':'Neha Rani Panjwani','seat':'12B','type':'Adult'}]
        people=reconcile_people(ai,local,'Ajay Kumar Panjwani 12A Neha Rani Panjwani 12B',('seat','type'))
        self.assertEqual([x['name'] for x in people],['Ajay Kumar Panjwani','Neha Rani Panjwani'])

    def test_supplier_surname_first_name_is_supported(self):
        source='SAHU / KOMALKANT KUMAR MR ADT'
        self.assertEqual(best_source_name('Komalkant','Komalkant Kumar Sahu',source),'Komalkant Kumar Sahu')

    def test_bus_extraction_restores_full_names_and_omitted_rows(self):
        source=('Bus PNR: BUS123\nFrom: Raipur\nTo: Nagpur\nJourney Date: 11 Sep 2026\n'
                '1 Mr. Ajay Kumar Panjwani Adult 12A\n2 Ms. Neha Rani Panjwani Adult 12B')
        remote={'booking_id':'','booking_date':'','pnr':'BUS123','status':'CONFIRMED','mobile':'',
                'operator':'','bus_number':'','bus_type':'','dep_time':'','dep_city':'Raipur',
                'dep_date':'11 Sep 2026','boarding_point':'','arr_time':'','arr_city':'Nagpur',
                'arr_date':'','drop_point':'','duration':'','passengers':[
                    {'name':'Ajay Panjwani','title':'Mr.','seat':'12A','type':'Adult','dob':'','boarding':''}],
                'base_fare':0,'taxes':0}
        with patch('bus_ticket.complete_json',return_value=remote):
            data=extract_bus_ticket([],source,None,None)
        self.assertEqual([x['name'] for x in data['passengers']],
                         ['Ajay Kumar Panjwani','Neha Rani Panjwani'])

    def test_air_extraction_restores_full_middle_name(self):
        source=('Airline PNR: ABC123\nPassenger Information\n1 Mr. Komalkant Kumar Sahu Adult\n'
                'Flight 6E 659\nDeparture DEL 05:55 11 Sep 2026\n'
                'Arrival RPR 07:55 11 Sep 2026\nBaggage Check-in 15kg Cabin 7kg')
        segment={'flight':'IndiGo','flight_number':'6E 659','aircraft':'','cabin':'','fare_type':'',
                 'dep_time':'05:55','dep_city':'Delhi','dep_code':'DEL','dep_date':'11 Sep 2026',
                 'dep_airport':'Delhi Airport','dep_terminal':'','arr_time':'07:55','arr_city':'Raipur',
                 'arr_code':'RPR','arr_date':'11 Sep 2026','arr_airport':'Raipur Airport',
                 'arr_terminal':'','duration':'2h','stops':'','layover':''}
        remote={'booking_id':'','booking_date':'','airline_pnr':'ABC123','gds_pnr':'',
                'status':'Confirmed','mobile':'','baggage_summary':'Check-in 15kg Cabin 7kg',
                'special_ancillary_summary':'','segments':[segment],'passengers':[
                    {'name':'Komalkant Sahu','title':'Mr.','ticket_number':'','type':'Adult',
                     'dob':'','baggage':'','special_ancillary':''}],
                'base_fare':0,'taxes':0,'gross_total':0,'payment_items':[]}
        with patch('flight_extractor.complete_json',return_value=remote):
            data=extract_flight_ticket([],source,None,None)
        self.assertEqual(data['passengers'][0]['name'],'Komalkant Kumar Sahu')

    def test_hotel_extraction_restores_full_guest_name(self):
        source=('Guest Name: Mr. Nikhil Raj Agrawal\nHotel Name: Test Hotel\nCity: Raipur\n'
                'Address: Main Road Raipur\nCheck-in: 11 Sep 2026\nCheck-out: 13 Sep 2026')
        remote={'reservation_id':'','guest_name':'Nikhil Agrawal','mobile':'','hotel_name':'Test Hotel',
                'hotel_address':'Main Road Raipur','hotel_city':'Raipur','check_in':'11 Sep 2026',
                'check_out':'13 Sep 2026','nights':'2','room_type':'','occupancy_summary':'',
                'room_count':1,'extra_bed_count':0,'meal_plan':'','base_fare':0,'taxes':0,
                'terms':[],'cost_components':[]}
        with patch('hotel_voucher.complete_json',return_value=remote):
            data=extract_hotel_voucher([],source,None,None)
        self.assertEqual(data['guest_name'],'Nikhil Raj Agrawal')


if __name__=='__main__': unittest.main()
