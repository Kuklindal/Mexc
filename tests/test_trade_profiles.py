import os
import unittest
from unittest.mock import patch

from trade_profiles import (eflp_p1_profiles, profile_from_env, ready_p1_profiles,
                            settings_for_profile, validate_unique_profiles)


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
    def test_dedicated_eflp_p1_accounts_are_separate_from_p2(self):
        env = configuration(2)
        env.update({'EFLP_P1_PROFILES': 'p1,p1_2',
                    'MEXC_P1_2_API_KEY': 'second-maker-key',
                    'MEXC_P1_2_SECRET_KEY': 'second-maker-secret',
                    'MEXC_P1_2_MEMBER_ID': 'second-maker-member',
                    'MEXC_P1_2_NICKNAME': 'second-maker',
                    'MEXC_P1_2_SELL_ADV_NO': 'a1234567890123456788',
                    'MEXC_P1_2_ADSPOWER_PROFILE_ID': 'second-maker-browser'})
        self.assertEqual(eflp_p1_profiles(env), ['p1', 'p1_2'])
        self.assertEqual([item.key for item in ready_p1_profiles(
            eflp_p1_profiles(env), env, include_main=False)], ['p1', 'p1_2'])
        self.assertEqual(profile_from_env('p1_2', env).member_id, 'second-maker-member')
        with patch.dict(os.environ, env):
            self.assertEqual(settings_for_profile('p1_2').api_key, 'second-maker-key')
        with self.assertRaisesRegex(ValueError, 'EFLP_P1_PROFILES'):
            eflp_p1_profiles({'EFLP_P1_PROFILES': 'p1,p1,p2_2'})

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
