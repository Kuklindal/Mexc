import unittest

from trade_profiles import profile_from_env, ready_p1_profiles, validate_unique_profiles


def configuration(count=20):
    env = {'MEXC_P1_API_KEY': 'main-key', 'MEXC_P1_SECRET_KEY': 'main-secret',
           'MEXC_P1_MEMBER_ID': 'main-member', 'MEXC_P1_NICKNAME': 'main',
           'MEXC_P1_SELL_ADV_NO': 'a1234567890123456789',
           'ADSPOWER_P1_PROFILE_ID': 'main-browser'}
    for index in range(count):
        prefix = f'MEXC_P2_{index + 1}'
        env.update({f'{prefix}_API_KEY': f'key-{index}',
                    f'{prefix}_SECRET_KEY': f'secret-{index}',
                    f'{prefix}_MEMBER_ID': f'member-{index}',
                    f'{prefix}_NICKNAME': f'name-{index}',
                    f'{prefix}_SELL_ADV_NO': f'a{index + 1:019d}',
                    f'{prefix}_ADSPOWER_PROFILE_ID': f'browser-{index}'})
    return env


class TradeProfileTests(unittest.TestCase):
    def test_dynamic_p1_and_twenty_distinct_p2(self):
        env = configuration(21)
        p1, p2 = validate_unique_profiles('1', [str(i) for i in range(2, 22)], env)
        self.assertEqual(p1.member_id, 'member-0')
        self.assertEqual(len(p2), 20)
        self.assertEqual(profile_from_env('p1', env).adspower_profile_id, 'main-browser')

    def test_missing_browser_or_ad_is_not_selectable(self):
        env = configuration(2)
        del env['MEXC_P2_2_ADSPOWER_PROFILE_ID']
        self.assertEqual([profile.key for profile in ready_p1_profiles(['1', '2'], env)],
                         ['p1', '1'])
        with self.assertRaisesRegex(ValueError, 'MEXC_P2_2_ADSPOWER_PROFILE_ID'):
            profile_from_env('2', env)

    def test_unique_selection_rejects_same_account_twice(self):
        env = configuration(21)
        with self.assertRaisesRegex(ValueError, 'разных П2'):
            validate_unique_profiles('1', ['2'] * 20, env)
