import unittest

from services.profile_fields import experience_error, rubros_error


class ProfileRulesTest(unittest.TestCase):
    def test_experience_uses_an_inclusive_integer_range(self):
        for value in ("0", "12", "100", "199", "200"):
            with self.subTest(value=value):
                self.assertEqual(experience_error(value), "")
        for value in ("201", "202", "500", "999", "1000", "8" * 5000):
            with self.subTest(length=len(value), prefix=value[:5]):
                self.assertIn("Fuera de rango", experience_error(value))

    def test_experience_rejects_fractions_negative_values_and_letters(self):
        for value in ("", "-1", "1.5", "1,5", "2e1", "abc", "٠٢"):
            with self.subTest(value=value):
                self.assertTrue(experience_error(value))

    def test_rubros_accepts_names_codes_and_mixed_comma_lists(self):
        for value in ("Cemento", "Diésel, lubricantes", "3011", "3011, 1510", "Acero A-36, 3011", "2 técnicos"):
            with self.subTest(value=value):
                self.assertEqual(rubros_error(value), "")

    def test_rubros_rejects_invalid_list_items_even_with_a_valid_neighbor(self):
        for value in ("", "-----", "Acero, 1.1", "3011.5151", "1.1", "Acero 2.5", "-3011",
                      "Acero,,cemento", "3011 1510", "3011, %", "x" * 501):
            with self.subTest(value=value):
                self.assertTrue(rubros_error(value))
