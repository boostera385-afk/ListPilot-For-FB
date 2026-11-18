import unittest

from core.utils.queue_rows import split_duplicate_rows


class DuplicateRowTests(unittest.TestCase):
    def rows(self, *names):
        return [{"rowNumber": index + 2, "id": name, "profileName": name} for index, name in enumerate(names)]

    def test_same_name_twice_keeps_the_first_row(self):
        keep, duplicates = split_duplicate_rows(self.rows("1 Oct Id 1 ali", "1 Oct Id 2 ali", "1 Oct Id 1 ali"))
        self.assertEqual([p["rowNumber"] for p in keep], [2, 3])
        self.assertEqual([(p["rowNumber"], first) for p, first in duplicates], [(4, 2)])

    def test_case_and_extra_spaces_do_not_make_a_new_profile(self):
        keep, duplicates = split_duplicate_rows(self.rows("6 AUG ID 2", " 6 aug  id 2"))
        self.assertEqual(len(keep), 1)
        self.assertEqual(duplicates[0][1], 2)

    def test_different_profiles_are_all_kept(self):
        keep, duplicates = split_duplicate_rows(self.rows("19 june id 7", "6 aug id 2", "1 Oct Id 1 ali"))
        self.assertEqual(len(keep), 3)
        self.assertEqual(duplicates, [])

    def test_id_and_name_of_one_profile_are_matched_through_the_resolver(self):
        posts = [{"rowNumber": 2, "id": "uuid-1", "profileName": ""},
                 {"rowNumber": 3, "id": "Seller A", "profileName": "Seller A"}]
        resolver = lambda value: "uuid-1" if value in ("uuid-1", "Seller A") else ""
        keep, duplicates = split_duplicate_rows(posts, resolve=resolver)
        self.assertEqual([p["rowNumber"] for p in keep], [2])
        self.assertEqual(duplicates[0][1], 2)

    def test_a_failing_resolver_never_blocks_the_queue(self):
        def boom(_):
            raise RuntimeError("Incogniton down")
        keep, duplicates = split_duplicate_rows(self.rows("A", "B"), resolve=boom)
        self.assertEqual(len(keep), 2)
        self.assertEqual(duplicates, [])

    def test_empty_queue(self):
        self.assertEqual(split_duplicate_rows([]), ([], []))
        self.assertEqual(split_duplicate_rows(None), ([], []))


if __name__ == "__main__":
    unittest.main()
