from __future__ import annotations

from django.test import TestCase

from projects.services import get_case_position
from projects.tests.helpers import make_case, make_project, make_run, make_user


class GetCasePositionTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def test_single_case_is_position_one_of_one(self) -> None:
        case = make_case(project=self.project)
        pivot = self.test_run.pivot_entries.create(test_case=case)

        position, total = get_case_position(pivot)

        self.assertEqual((position, total), (1, 1))

    def test_position_follows_creation_order(self) -> None:
        first_case = make_case(project=self.project, title="First")
        second_case = make_case(project=self.project, title="Second")
        third_case = make_case(project=self.project, title="Third")
        first_pivot = self.test_run.pivot_entries.create(test_case=first_case)
        second_pivot = self.test_run.pivot_entries.create(test_case=second_case)
        third_pivot = self.test_run.pivot_entries.create(test_case=third_case)

        self.assertEqual(get_case_position(first_pivot), (1, 3))
        self.assertEqual(get_case_position(second_pivot), (2, 3))
        self.assertEqual(get_case_position(third_pivot), (3, 3))

    def test_position_is_scoped_to_its_own_run(self) -> None:
        case = make_case(project=self.project)
        other_run = make_run(project=self.project)
        # Give the other run two earlier pivots that must not affect this
        # pivot's position, since position is scoped to its own run.
        other_run.pivot_entries.create(test_case=make_case(project=self.project))
        other_run.pivot_entries.create(test_case=make_case(project=self.project))
        pivot = self.test_run.pivot_entries.create(test_case=case)

        position, total = get_case_position(pivot)

        self.assertEqual((position, total), (1, 1))
