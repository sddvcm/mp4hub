"""Requirement coverage: resume policy, portable data directory, folder tree,
and the bulk actions used by 观看历史 / 继续观看 / 收藏."""
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_stability  # Initializes the isolated test data directory.
from app import main as m
from app import preferences as preferences_module
from fastapi import HTTPException


class ResumePolicyTests(unittest.TestCase):
    def setUp(self):
        with m.connection() as db: db.execute('DELETE FROM preferences')

    def test_resume_mode_accepts_only_the_three_documented_values(self):
        for value in ('restart', 'resume', 'ask'):
            m.set_preferences(m.PreferencesInput(values={'resumeMode': value}))
            self.assertEqual(m.get_preferences()['values']['resumeMode'], value)
        for value in ('always', 'yes', True, None, 1, '', 'RESUME'):
            with self.assertRaises(HTTPException):
                m.set_preferences(m.PreferencesInput(values={'resumeMode': value}))

    def test_resume_mode_defaults_are_absent_until_saved(self):
        # The client falls back to 'ask'; the backend must not invent a value.
        self.assertNotIn('resumeMode', m.get_preferences()['values'])

    def test_unknown_resume_value_in_database_is_skipped_not_fatal(self):
        with m.connection() as db:
            db.execute("INSERT INTO preferences VALUES('resumeMode',?,0)", (json.dumps('bogus'),))
        self.assertNotIn('resumeMode', m.get_preferences()['values'])

    def test_resume_mode_is_included_in_backup(self):
        m.set_preferences(m.PreferencesInput(values={'resumeMode': 'restart'}))
        response = m.create_backup()
        try:
            import sqlite3
            db = sqlite3.connect(response.path)
            try:
                self.assertEqual(json.loads(db.execute(
                    "SELECT value FROM preferences WHERE key='resumeMode'").fetchone()[0]), 'restart')
            finally:
                db.close()
        finally:
            response.background.func(*response.background.args, **response.background.kwargs)


class PortableDataDirectoryTests(unittest.TestCase):
    def test_source_checkout_defaults_to_project_root_data(self):
        # Without the environment override the non-frozen service must use
        # <project>/data so the library travels with the checkout.
        with patch.dict(m.os.environ, {}, clear=False):
            m.os.environ.pop('AVHUB_DATA_DIR', None)
            resolved = m.data_directory()
        self.assertEqual(resolved, (m.APP_HOME / 'data').resolve())

    def test_frozen_build_uses_avhub_data_beside_the_executable(self):
        with patch.dict(m.os.environ, {}, clear=False), patch.object(m, 'FROZEN', True):
            m.os.environ.pop('AVHUB_DATA_DIR', None)
            resolved = m.data_directory()
        self.assertEqual(resolved, (m.APP_HOME / 'AVHub-data').resolve())

    def test_environment_override_wins_over_program_directory(self):
        with patch.dict(m.os.environ, {'AVHUB_DATA_DIR': str(m.DATA)}):
            self.assertEqual(m.data_directory(), m.DATA)

    def test_unwritable_program_directory_falls_back_without_raising(self):
        with patch.dict(m.os.environ, {}, clear=False), patch.object(m, '_writable', return_value=False):
            m.os.environ.pop('AVHUB_DATA_DIR', None)
            fallback = m.data_directory()
        self.assertNotEqual(fallback, (m.APP_HOME / 'data').resolve())
        self.assertIn('AVHub', str(fallback))

    def test_reported_location_exposes_source_and_writability(self):
        info = m.data_location()
        self.assertEqual(info['data_dir'], str(m.DATA))
        self.assertIn(info['source'], ('env', 'portable', 'fallback'))
        self.assertIsInstance(info['writable'], bool)


class FolderTreeTests(unittest.TestCase):
    def setUp(self):
        self.root = m.DATA / 'tree-root'
        with m.connection() as db:
            db.execute('DELETE FROM media')
            db.execute('DELETE FROM roots')
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(1,?,0)', (str(self.root),))
        self.add(1, 'root.mp4')
        self.add(2, 'Drama/first.mp4')
        self.add(3, 'Drama/Season 1/second.mkv')
        self.add(4, 'Drama/Season 1/third.mkv')
        self.add(5, 'Drama/Season 2/fourth.mkv')
        self.add(6, 'Other/fifth.mp4')
        self.add(7, 'Drama/offline/gone.mp4', missing=1)

    def add(self, media_id, path, missing=0):
        full = self.root.joinpath(*path.split('/'))
        with m.connection() as db:
            db.execute('''INSERT INTO media(id,path,root_id,name,title,ext,duration,created_at,updated_at,missing)
                VALUES(?,?,1,?,?,?,120,0,0,?)''',
                       (media_id, str(full), full.name, full.stem, full.suffix, missing))

    def entry(self, tree, folder):
        return next((item for item in tree['folders'] if item['folder'] == folder), None)

    def test_children_carry_subtree_and_direct_counts(self):
        tree = m.folder_tree()
        drama = self.entry(tree, 'Drama')
        season = self.entry(tree, 'Season 1' if False else 'Drama/Season 1')
        self.assertEqual((drama['count'], drama['direct_count']), (4, 1))
        self.assertEqual((season['count'], season['direct_count']), (2, 2))

    def test_root_level_files_are_excluded_from_every_folder_count(self):
        tree = m.folder_tree()
        self.assertEqual(tree['roots'][0]['count'], 6)
        self.assertEqual(tree['roots'][0]['direct_count'], 1)
        self.assertNotIn('', [item['folder'] for item in tree['folders']])

    def test_missing_videos_are_not_counted_and_no_disk_access_happens(self):
        with patch.object(Path, 'is_dir', side_effect=AssertionError('disk access')), \
                patch.object(Path, 'exists', side_effect=AssertionError('disk access')):
            tree = m.folder_tree()
        self.assertEqual(tree['total'], 6)

    def test_parents_are_returned_before_their_children(self):
        folders = [item['folder'] for item in m.folder_tree()['folders']]
        self.assertLess(folders.index('Drama'), folders.index('Drama/Season 1'))
        self.assertEqual(folders.index('Drama/Season 1'), folders.index('Drama/Season 2') - 1)

    def test_unknown_root_is_rejected(self):
        with self.assertRaises(HTTPException):
            m.folder_tree(root_id=999)

    def test_root_filter_limits_results(self):
        with m.connection() as db:
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(2,?,0)', (str(m.DATA / 'other-root'),))
        self.assertEqual(m.folder_tree(root_id=2)['folders'], [])
        # Drama / Other / Drama/Season 1 / Drama/Season 2 — the missing-only
        # Drama/offline folder is excluded by design.
        self.assertEqual(len(m.folder_tree(root_id=1)['folders']), 4)

    def test_container_folders_without_direct_videos_are_still_listed(self):
        # 电影/国语 holds only the 2005 and 2006 sub-folders. The sidebar must
        # still render 国语 as its own expandable level, so a folder that owns no
        # file directly cannot be dropped from the tree.
        self.add(20, 'Drama/Season 3/episode.mkv')
        tree = m.folder_tree()
        names = {item['folder'] for item in tree['folders']}
        self.assertIn('Drama/Season 3', names)
        self.assertIn('Drama', names)
        season3 = self.entry(tree, 'Drama/Season 3')
        self.assertEqual((season3['count'], season3['direct_count']), (1, 1))
        self.assertEqual(season3['depth'], 1)

    def test_nested_levels_keep_parent_before_child(self):
        self.add(21, 'Other/Sub/Deep/clip.mkv')
        folders = [item['folder'] for item in m.folder_tree()['folders']]
        self.assertLess(folders.index('Other'), folders.index('Other/Sub'))
        self.assertLess(folders.index('Other/Sub'), folders.index('Other/Sub/Deep'))
        deep = self.entry(m.folder_tree(), 'Other/Sub/Deep')
        self.assertEqual((deep['count'], deep['direct_count'], deep['depth']), (1, 1, 2))


class BulkActionTests(unittest.TestCase):
    def setUp(self):
        with m.connection() as db:
            db.execute('DELETE FROM media')
            db.execute('''INSERT INTO media(id,path,name,title,duration,progress,watched,manual_watched,favorite,created_at,updated_at)
                          VALUES(1,'a.mp4','a','a',120,60,0,NULL,0,0,0)''')
            db.execute('''INSERT INTO media(id,path,name,title,duration,progress,watched,manual_watched,favorite,created_at,updated_at)
                          VALUES(2,'b.mp4','b','b',120,30,0,NULL,0,0,0)''')

    def act(self, ids, action):
        return m.batch_action(m.BatchActionInput(media_ids=ids, action=action))

    def test_bulk_favorite_and_unfavorite_toggle_both_rows(self):
        self.assertEqual(self.act([1, 2], 'favorite')['updated'], 2)
        self.assertTrue(all(row['favorite'] for row in m.media(limit=10)))
        self.act([1, 2], 'unfavorite')
        self.assertFalse(any(row['favorite'] for row in m.media(limit=10)))

    def test_bulk_clear_history_keeps_favorites_tags_and_rows(self):
        self.act([1], 'favorite')
        result = self.act([1, 2], 'clear_history')
        self.assertEqual(result['updated'], 2)
        rows = {row['id']: row for row in m.media(limit=10)}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['progress'], 0)
        self.assertIsNone(rows[1]['last_played'])
        self.assertTrue(rows[1]['favorite'])
        self.assertEqual(m.media(view='history', limit=10), [])

    def test_bulk_clear_history_removes_rows_from_continue_watching(self):
        self.assertEqual(len(m.media(view='continue', limit=10)), 2)
        self.act([1, 2], 'clear_history')
        self.assertEqual(m.media(view='continue', limit=10), [])

    def test_manual_marks_survive_clearing_history(self):
        self.act([1], 'mark_watched')
        self.act([1, 2], 'clear_history')
        row = next(row for row in m.media(limit=10) if row['id'] == 1)
        self.assertTrue(row['watched'])

    def test_bulk_marks_and_restores_automatic_judgement(self):
        self.act([1, 2], 'mark_watched')
        self.assertTrue(all(row['watched'] for row in m.media(limit=10)))
        self.act([1, 2], 'mark_unwatched')
        self.assertFalse(any(row['watched'] for row in m.media(limit=10)))
        self.act([1, 2], 'reset_watched')
        # 60/120 = 50% and 30/120 = 25% are both below the 92% threshold.
        self.assertFalse(any(row['watched'] for row in m.media(limit=10)))

    def test_reset_watched_marks_a_finished_video(self):
        with m.connection() as db:
            db.execute('UPDATE media SET progress=duration WHERE id=1')
        self.act([1], 'mark_unwatched')
        self.act([1], 'reset_watched')
        self.assertTrue(next(row for row in m.media(limit=10) if row['id'] == 1)['watched'])

    def test_missing_ids_abort_the_whole_batch(self):
        with self.assertRaises(HTTPException):
            self.act([1, 999], 'favorite')
        self.assertFalse(any(row['favorite'] for row in m.media(limit=10)))

    def test_duplicate_ids_are_processed_once(self):
        self.assertEqual(self.act([1, 1, 1], 'favorite')['updated'], 1)

    def test_batch_size_is_bounded_to_five_hundred(self):
        with self.assertRaises(Exception):
            m.BatchActionInput(media_ids=list(range(1, 502)), action='favorite')

    def test_invalid_action_is_rejected(self):
        with self.assertRaises(Exception):
            m.BatchActionInput(media_ids=[1], action='delete_source')


class DesktopPickerTests(unittest.TestCase):
    """The frozen desktop backend has no tkinter, so the renderer forwards the
    folder returned by Electron's native dialog. These tests pin both routes:
    a supplied path is consumed directly, an absent one falls back to tkinter.
    """

    def setUp(self):
        self.directory = m.DATA / 'picker-target'
        self.directory.mkdir(parents=True, exist_ok=True)
        with m.connection() as db:
            db.execute('DELETE FROM media')
            db.execute('DELETE FROM roots')

    def test_pick_root_consumes_supplied_path_without_tkinter(self):
        with patch.dict('sys.modules', {'tkinter': None, 'tkinter.filedialog': None}):
            root = m.pick_root(m.PickInput(path=str(self.directory)))
        self.assertEqual(Path(root['path']), self.directory)
        self.assertEqual([item['path'] for item in m.roots()], [str(self.directory)])

    def test_pick_root_rejects_a_missing_folder(self):
        with self.assertRaises(HTTPException) as caught:
            m.pick_root(m.PickInput(path=str(self.directory / 'ghost')))
        self.assertEqual(caught.exception.status_code, 400)

    def test_pick_root_without_body_still_uses_tkinter(self):
        # Browser builds keep the old behaviour; the desktop build never gets here.
        fake = types.ModuleType('tkinter')
        fake.filedialog = types.SimpleNamespace(askdirectory=lambda **_: str(self.directory))
        fake.Tk = lambda: types.SimpleNamespace(
            withdraw=lambda: None, attributes=lambda *a: None, destroy=lambda: None)
        with patch.dict('sys.modules', {'tkinter': fake, 'tkinter.filedialog': fake.filedialog}):
            result = m.pick_root(None)
        self.assertEqual(result['path'], str(self.directory))

    def test_tkinter_picker_reopens_at_the_most_recent_media_directory(self):
        """需求 2：第二次添加媒体目录时，对话框应停在上次选过的位置。"""
        older = m.DATA / 'picker-older'
        older.mkdir(parents=True, exist_ok=True)
        with m.connection() as db:
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(1,?,100)', (str(older),))
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(2,?,200)', (str(self.directory),))
        seen = {}
        fake = types.ModuleType('tkinter')
        def askdirectory(**kwargs):
            seen.update(kwargs)
            return str(self.directory)
        fake.filedialog = types.SimpleNamespace(askdirectory=askdirectory)
        fake.Tk = lambda: types.SimpleNamespace(
            withdraw=lambda: None, attributes=lambda *a: None, destroy=lambda: None)
        with patch.dict('sys.modules', {'tkinter': fake, 'tkinter.filedialog': fake.filedialog}):
            m.pick_root(None)
        # added_at 更大的是最近添加的目录，应作为 initialdir。
        self.assertEqual(seen.get('initialdir'), str(self.directory))

    def test_tkinter_picker_omits_initialdir_when_no_usable_root_exists(self):
        """没有任何可用媒体目录时不应传 initialdir，避免对话框开到无效路径。"""
        seen = {}
        fake = types.ModuleType('tkinter')
        def askdirectory(**kwargs):
            seen.update(kwargs)
            return str(self.directory)
        fake.filedialog = types.SimpleNamespace(askdirectory=askdirectory)
        fake.Tk = lambda: types.SimpleNamespace(
            withdraw=lambda: None, attributes=lambda *a: None, destroy=lambda: None)
        with patch.dict('sys.modules', {'tkinter': fake, 'tkinter.filedialog': fake.filedialog}):
            m.pick_root(None)
        self.assertIsNone(seen.get('initialdir'))

    def test_tkinter_picker_ignores_a_root_whose_folder_is_gone(self):
        """记录的目录已被删除时必须退回无 initialdir，否则对话框会开到空白位置。"""
        missing = m.DATA / 'picker-vanished'
        with m.connection() as db:
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(1,?,999)', (str(missing),))
        seen = {}
        fake = types.ModuleType('tkinter')
        def askdirectory(**kwargs):
            seen.update(kwargs)
            return str(self.directory)
        fake.filedialog = types.SimpleNamespace(askdirectory=askdirectory)
        fake.Tk = lambda: types.SimpleNamespace(
            withdraw=lambda: None, attributes=lambda *a: None, destroy=lambda: None)
        with patch.dict('sys.modules', {'tkinter': fake, 'tkinter.filedialog': fake.filedialog}):
            m.pick_root(None)
        self.assertIsNone(seen.get('initialdir'))

    def test_pick_relocation_consumes_supplied_path(self):
        with m.connection() as db:
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(1,?,0)', (str(self.directory),))
        target = m.DATA / 'picker-elsewhere'
        target.mkdir(parents=True, exist_ok=True)
        result = m.pick_relocation(1, m.PickInput(path=str(target)))
        self.assertEqual((result['id'], Path(result['path'])), (1, target))

    def test_pick_relocation_rejects_an_unknown_root_before_touching_the_path(self):
        with self.assertRaises(HTTPException) as caught:
            m.pick_relocation(999, m.PickInput(path=str(self.directory)))
        self.assertEqual(caught.exception.status_code, 404)

    def test_pick_screenshot_directory_consumes_supplied_path(self):
        self.assertEqual(m.pick_screenshot_directory(m.PickInput(path=str(self.directory))),
                         {'directory': str(self.directory)})

    def test_pick_screenshot_directory_without_body_reports_the_tkinter_failure(self):
        with patch.dict('sys.modules', {'tkinter': None, 'tkinter.filedialog': None}):
            with self.assertRaises(HTTPException) as caught:
                m.pick_screenshot_directory(None)
        self.assertEqual(caught.exception.status_code, 500)


class LibraryAllActionTests(unittest.TestCase):
    """Whole-view actions: no id list, the row set comes from view + root scope."""

    def setUp(self):
        with m.connection() as db:
            db.execute('DELETE FROM media')
            db.execute('DELETE FROM roots')
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(1,?,0)', (str(m.DATA / 'r1'),))
            db.execute('INSERT INTO roots(id,path,added_at) VALUES(2,?,0)', (str(m.DATA / 'r2'),))
            # 1,2 in root 1; 3,4 in root 2. 2 and 4 carry history; 2 starts favourited.
            rows = [(1, 1, 0, 0, 0, None), (2, 1, 1, 60, 0, 100.0),
                    (3, 2, 0, 0, 0, None), (4, 2, 0, 30, 0, 200.0)]
            for media_id, root_id, favorite, progress, watched, last in rows:
                db.execute('''INSERT INTO media(id,path,root_id,name,title,ext,duration,progress,watched,
                    favorite,last_played,created_at,updated_at,missing)
                    VALUES(?,?,?,?,?,?,120,?,?,?,?,0,0,0)''',
                           (media_id, f'C:/lib/f{media_id}.mp4', root_id, f'f{media_id}', f'f{media_id}',
                            '.mp4', progress, watched, favorite, last))

    def act(self, view, action, root_id=None):
        return m.library_all_action(m.LibraryAllActionInput(view=view, action=action, root_id=root_id))

    def rows(self):
        return {row['id']: row for row in m.media(limit=20)}

    def test_favorite_hits_every_row_in_the_library(self):
        self.assertEqual(self.act('all', 'favorite')['updated'], 4)
        self.assertTrue(all(row['favorite'] for row in self.rows().values()))

    def test_unfavorite_only_touches_the_favorites_view(self):
        self.act('all', 'favorite')
        m.set_favorite(1, m.FavoriteInput(favorite=False))
        # 收藏 view now holds 2,3,4; 1 must keep favorite=0 and the rest flip too.
        self.assertEqual(self.act('favorites', 'unfavorite')['updated'], 3)
        self.assertFalse(any(row['favorite'] for row in self.rows().values()))

    def test_root_scope_limits_the_action_to_one_media_directory(self):
        self.assertEqual(self.act('all', 'favorite', root_id=2)['updated'], 2)
        values = {media_id: row['favorite'] for media_id, row in self.rows().items()}
        # Root 1 keeps its own state: 1 was never favourited, 2 already was.
        self.assertEqual(values, {1: 0, 2: 1, 3: 1, 4: 1})

    def test_history_view_clears_only_rows_with_playback_history(self):
        # 观看历史 only lists 2 and 4, so 1 and 3 stay untouched.
        self.assertEqual(self.act('history', 'clear_history')['updated'], 2)
        values = {media_id: (row['progress'], row['last_played']) for media_id, row in self.rows().items()}
        self.assertEqual(values[2], (0, None))
        self.assertEqual(values[4], (0, None))
        self.assertEqual(values[1], (0, None))
        self.assertEqual(values[3], (0, None))

    def test_clear_history_keeps_favorites_and_manual_marks(self):
        self.act('all', 'favorite')
        self.act('all', 'mark_watched')
        self.act('all', 'clear_history')
        for row in self.rows().values():
            self.assertTrue(row['favorite'])
            self.assertTrue(row['watched'])

    def test_mark_actions_apply_library_wide(self):
        self.assertEqual(self.act('all', 'mark_watched')['updated'], 4)
        self.assertTrue(all(row['watched'] for row in self.rows().values()))
        self.assertEqual(self.act('all', 'mark_unwatched')['updated'], 4)
        self.assertFalse(any(row['watched'] for row in self.rows().values()))

    def test_movies_and_series_views_filter_by_kind(self):
        with m.connection() as db:
            db.execute("UPDATE media SET kind='movie' WHERE id IN (1,2)")
            db.execute("UPDATE media SET kind='episode' WHERE id IN (3,4)")
        self.assertEqual(self.act('movies', 'favorite')['updated'], 2)
        values = {media_id: row['favorite'] for media_id, row in self.rows().items()}
        self.assertEqual(values, {1: 1, 2: 1, 3: 0, 4: 0})
        self.assertEqual(self.act('series', 'favorite')['updated'], 2)
        self.assertTrue(all(row['favorite'] for row in self.rows().values()))

    def test_an_unknown_view_is_rejected(self):
        with self.assertRaises(Exception):
            m.LibraryAllActionInput(view='playlists', action='favorite')

    def test_empty_view_reports_zero_without_error(self):
        with m.connection() as db:
            db.execute('UPDATE media SET favorite=0')
        self.assertEqual(self.act('favorites', 'unfavorite')['updated'], 0)


class SortOrderTests(unittest.TestCase):
    """Resolution and file-size sorts added alongside the existing orders."""

    def setUp(self):
        with m.connection() as db:
            db.execute('DELETE FROM media')
            db.execute('DELETE FROM roots')
            # Sizes and pixel counts are deliberately uncorrelated so a mixed-up
            # column mapping cannot pass by accident.
            rows = [
                (1, 'small_lowres.mp4', 100, 640, 480),
                (2, 'big_highres.mp4', 900, 1920, 1080),
                (3, 'mid_wide.mp4', 500, 1920, 800),
                (4, 'unknown.mp4', 0, None, None),
            ]
            for media_id, name, size, width, height in rows:
                db.execute('''INSERT INTO media(id,path,name,title,ext,size,width,height,duration,
                    created_at,updated_at,missing) VALUES(?,?,?,?,?,?,?,?,120,0,0,0)''',
                           (media_id, f'C:/lib/{name}', name, name, '.mp4', size, width, height))

    def order(self, sort):
        return [row['id'] for row in m.media(limit=10, sort=sort)]

    def test_resolution_desc_uses_pixel_count_and_sinks_unknown(self):
        # 1920x1080 (2.07M) > 1920x800 (1.54M) > 640x480 (0.31M) > unknown.
        self.assertEqual(self.order('resolution_desc'), [2, 3, 1, 4])

    def test_resolution_asc_is_the_reverse_with_unknown_still_last(self):
        self.assertEqual(self.order('resolution_asc'), [1, 3, 2, 4])

    def test_size_desc_orders_by_bytes_and_sinks_unknown(self):
        self.assertEqual(self.order('size_desc'), [2, 3, 1, 4])

    def test_size_asc_is_the_reverse_with_unknown_still_last(self):
        self.assertEqual(self.order('size_asc'), [1, 3, 2, 4])

    def test_existing_orders_still_work(self):
        self.assertEqual(self.order('name'), [2, 3, 1, 4])
        self.assertEqual(self.order('duration_desc'), [1, 2, 3, 4])


if __name__ == '__main__':
    unittest.main()
