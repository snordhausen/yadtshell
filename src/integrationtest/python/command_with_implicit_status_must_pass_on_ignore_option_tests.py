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

__author__ = 'Maximilien Riehl'

import unittest
import integrationtest_support

import yadt_status_answer


class Test (integrationtest_support.IntegrationTestSupport):

    def test(self):
        self.write_target_file('it01.domain', 'unreachable.host.domain')

        with self.fixture() as when:
            when.calling('ssh').at_least_with_arguments('it01.domain').and_input('/usr/bin/yadt-status') \
                .then_write(yadt_status_answer.stdout('it01.domain'))
            when.calling('ssh').at_least_with_arguments('it01.domain').then_return(0)
            when.calling('ssh').at_least_with_arguments('unreachable.host.domain').then_return(255)

        # "lock" command calls "status" implicitly and must pass on the
        # --ignore-unreachable-hosts option.
        lock_return_code = self.execute_command(
            'yadtshell lock host://* -m "locking the hosts" -v --no-final-status --ignore-unreachable-hosts')

        # --ignore-unreachable-hosts was not given, command must fail.
        self.assertEqual(0, lock_return_code)


if __name__ == '__main__':
    unittest.main()
