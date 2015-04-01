#   YADT - an Augmented Deployment Tool
#   Copyright (C) 2010-2014  Immobilien Scout GmbH
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

__author__ = 'Michael Gruber'

import unittest
import integrationtest_support


class Test (integrationtest_support.IntegrationTestSupport):

    def test(self):
        self.write_target_file('it01.domain')

        actual_return_code = self.execute_command(
            'yadtshell ignore service://* -m "ignoring" -v')

        self.assertEqual(1, actual_return_code)


if __name__ == '__main__':
    unittest.main()
