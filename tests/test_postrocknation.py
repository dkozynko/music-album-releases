import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from test_update import updater, bandcamp_document, wiki_payload
import postrocknation as prn


BASE = 'https://postrocknation.com'
ARCHIVE = BASE + '/page/release-archive'
PROFILE = BASE + '/bands/70/example-act'


def archive(rows, start=1, total=6):
    cells = ''.join(f'<tr><td><img></td><td><a href="{BASE}/albums/{identifier}/record-{identifier}">{title}</a></td><td><a href="{PROFILE}">Example Act</a></td><td>{kind}</td><td>{released}</td></tr>' for identifier,title,kind,released in rows)
    end = start + len(rows) - 1 if rows else 0
    return '<h2>Login to continue</h2><form><input name="password"></form><table class="fi-ta-table"><thead><tr><th></th><th>Name</th><th>Band</th><th>Release Type</th><th>Release Date</th></tr></thead><tbody>' + cells + f'</tbody></table><span>Showing {start if rows else 0} to {end} of {total:,} results</span>'


def profile(tags=('Post-rock','Ambient')):
    return f'<link rel="canonical" href="{PROFILE}"><h1>Example Act</h1><div><span>TAGS:</span>' + ''.join(f'<span>{tag}</span>' for tag in tags) + '</div><footer><span>Jazz</span></footer>'


def detail(identifier, released, title=None, kind='album'):
    title = title or f'Record {identifier}'
    return f'<link rel="canonical" href="{BASE}/albums/{identifier}/record-{identifier}"><h1>{title}</h1><div>released <span>{released}</span><span>{kind}</span></div><div>Artists: <a href="{PROFILE}">Example Act</a></div>'


def pages():
    return {ARCHIVE: archive(((1,'Record 1','album','2026-10-02'),(2,'Track','single','2026-10-01'))),
            ARCHIVE+'?page=2': archive(((3,'Record 3','album','2025-05-16'),(4,'Short (EP)','album','2025-03-12')),3),
            ARCHIVE+'?page=3': archive(((5,'Old','album','2024-12-20'),(6,'Older','album','2024-10-01')),5),
            PROFILE: profile(), BASE+'/albums/1/record-1':detail(1,'2026-10-02'),BASE+'/albums/3/record-3':detail(3,'2025-05-16')}


class FixtureClient:
    def __init__(self, data):self.data=data
    def get(self,url):
        if url not in self.data:raise AssertionError(f'Unexpected source request: {url}')
        value=self.data[url]
        if isinstance(value,Exception):raise value
        return value


class PostRockNationTests(unittest.TestCase):
    def test_archive_reads_columns_native_ids_and_real_pagination_despite_login_overlay(self):
        rows,start,end,total=prn.parse_archive(pages()[ARCHIVE])
        self.assertEqual((start,end,total),(1,2,6))
        self.assertEqual([(r['artist'],r['album'],r['release_date'],r['release_type']) for r in rows],
                         [('Example Act','Record 1','2026-10-02','album'),('Example Act','Track','2026-10-01','single')])
        self.assertEqual(rows[0]['source_ids'],['postrocknation:1'])
        with self.assertRaises(ValueError):prn.parse_archive('<h2>Login to continue</h2>')

    def test_profile_tags_are_scoped_and_detail_dates_are_not_the_discovery_year(self):
        self.assertEqual(set(prn.parse_profile(profile())),{'Post-rock','Ambient'})
        row=prn.parse_detail(detail(50,'2024-06'),50)
        self.assertEqual(row['release_date'],'2024-06')
        self.assertEqual(row['year'],2024)
        with self.assertRaises(ValueError):prn.parse_detail(detail(999,'2024-06'),50)

    def test_login_only_artist_profile_is_not_successful_empty_tag_coverage(self):
        body=f'<link rel="canonical" href="{PROFILE}"><h1>Login to continue</h1><form><input name="password"></form>'
        with self.assertRaises(ValueError):prn.parse_profile(body)

    def test_collector_walks_all_requested_years_and_stops_before_older_pages(self):
        rows,errors=prn.collect_postrocknation(FixtureClient(pages()),{2025,2026})
        self.assertEqual(errors,[])
        self.assertEqual([r['album'] for r in rows],['Record 1','Track','Record 3','Short (EP)'])
        self.assertEqual([r['release_type'] for r in rows],['album','single','album','ep'])
        self.assertEqual(set(rows[0]['styles']),{'Post-rock','Ambient'})
        self.assertEqual(len(updater().merge_catalog(rows)),2)

    def test_distinct_album_and_single_ids_with_the_same_name_do_not_exclude_the_album(self):
        m=updater();data=pages()
        data[ARCHIVE]=archive(((1,'Record 1','album','2026-10-02'),(2,'Record 1','single','2026-10-01')))
        rows,errors=prn.collect_postrocknation(FixtureClient(data),{2025,2026})
        self.assertEqual(errors,[])
        merged=m.merge_catalog(rows)
        self.assertEqual({r['album'] for r in merged},{'Record 1','Record 3'})
        album=next(r for r in merged if r['album']=='Record 1')
        self.assertIn('postrocknation:1',album['source_ids'])
        self.assertNotIn('postrocknation:2',album['source_ids'])
        changed=dict(rows[0],release_type='single')
        self.assertEqual(m.merge_catalog([changed],[album]),[])

    def test_distinct_formats_keep_synthetic_wiki_aliases_scoped_across_partial_updates(self):
        m=updater();url='https://example.bandcamp.com/album/new-album'
        lp={'artist':'Example Act','album':'Canonical Name','year':2026,'release_date':'2026-10-02','source_ids':['postrocknation:1']}
        single=dict(lp,release_type='single',release_date='2026-10-01',source_ids=['postrocknation:2'])
        wiki=dict(lp,source_ids=['reddit-wiki:2026:entry','bandcamp-url:'+url])
        wiki_single=dict(single,source_ids=wiki['source_ids'])
        primary=m.parse_bandcamp_album(bandcamp_document('02 Oct 2026 00:00:00 GMT'),url)
        rows=m.merge_catalog([lp,single,wiki,wiki_single,primary])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['album'],'New Album')
        self.assertIn('bandcamp:123',rows[0]['source_ids'])
        self.assertIn('postrocknation:1',rows[0]['source_ids'])
        self.assertNotIn('postrocknation:2',rows[0]['source_ids'])
        self.assertEqual(m.merge_catalog([single,wiki_single],rows),rows)

    def test_prn_single_does_not_erase_an_album_from_another_catalog_by_name_alone(self):
        m=updater()
        lp={'artist':'Example Act','album':'Record','year':2026,'release_date':'2026-10-02','source_ids':['discogs-master:70']}
        single=dict(lp,release_type='single',release_date='2026-09-01',source_ids=['postrocknation:2'])
        old=m.merge_catalog([lp])
        self.assertEqual(m.merge_catalog([single],old),old)

    def test_explicit_live_title_is_not_just_an_ambiguous_single_type(self):
        rows,_,_,_=prn.parse_archive(archive(((1,'Live à Montreuil','single','2026-10-02'),),1,1))
        self.assertEqual(rows[0]['release_type'],'live')

    def test_ignored_next_page_reports_incomplete_source_but_keeps_completed_rows(self):
        data=pages();data[ARCHIVE+'?page=2']=data[ARCHIVE]
        rows,errors=prn.collect_postrocknation(FixtureClient(data),{2025,2026})
        self.assertEqual(len(rows),2)
        self.assertEqual(len(errors),1)
        self.assertIn('pagination',errors[0].lower())

    def test_short_nonfinal_older_page_is_incomplete_before_the_year_stop(self):
        data=pages();data[ARCHIVE+'?page=2']=archive(((5,'Old','album','2024-12-20'),),3,6)
        rows,errors=prn.collect_postrocknation(FixtureClient(data),{2025,2026})
        self.assertEqual(len(rows),2)
        self.assertTrue(any('incomplete' in error.lower() for error in errors))

    def test_profile_failure_preserves_album_facts_and_other_pages(self):
        data=pages();data[PROFILE]=ValueError('HTTP 404')
        rows,errors=prn.collect_postrocknation(FixtureClient(data),{2025,2026})
        self.assertEqual(len(rows),4)
        self.assertTrue(any('404' in error for error in errors))
        self.assertEqual(rows[0]['styles'],[])

    def test_saved_id_refreshes_even_when_its_date_moves_before_the_selected_year(self):
        data={ARCHIVE:archive(((5,'Old','album','2024-12-20'),(6,'Older','album','2024-10-01')),1,2),PROFILE:profile(),BASE+'/albums/50/record-50':detail(50,'2024-06')}
        old={'artist':'Example Act','album':'Record 50','year':2026,'release_date':'2026-10-02','source_ids':['postrocknation:50'],'source_urls':[BASE+'/albums/50/record-50']}
        rows,errors=prn.collect_postrocknation(FixtureClient(data),{2026},[old])
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['release_date'],'2024-06')
        merged=updater().merge_catalog(rows,[old])
        self.assertEqual(merged[0]['date_status'],'conflict')
        self.assertEqual(merged[0]['date_candidates'],['2024-06','2026-10-02'])

    def test_cached_cli_outputs_and_repeat_are_stable(self):
        m=updater()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'cache';cache.mkdir();output=root/'output'
            (root/'sources.json').write_text(json.dumps({'postrocknation':True,'sputnikmusic':False,'reddit':False,'musicbrainz':False}))
            (root/'seeds.json').write_text('[]');(root/'corrections.json').write_text('[]')
            for url,body in pages().items():
                (cache/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps({'url':url,'body':body}))
            args=['--year','2025','2026','--offline','--config',str(root/'sources.json'),'--corrections',str(root/'corrections.json'),'--cache-dir',str(cache),'--output-dir',str(output)]
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(args),0)
                before={p.name:p.read_bytes() for p in output.iterdir()}
                self.assertEqual(m.main(args),0)
            self.assertEqual({p.name:p.read_bytes() for p in output.iterdir()},before)
            with (output/'2026.csv').open() as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['album'],'Record 1')
            self.assertEqual(set(json.loads(rows[0]['styles'])),{'Post-rock','Ambient'})
            self.assertIn('Post-Rock Nation',(output/'2026.md').read_text())


class BandcampReferenceTests(unittest.TestCase):
    def facts(self):
        m=updater();url='https://example.bandcamp.com/album/new-album'
        community=m.parse_reddit(wiki_payload('<tr><td>Oct 2</td><td>Different Act</td><td><a href="'+url+'">Different Record</a></td></tr>'),2026)[0]
        native=m.parse_bandcamp_album(bandcamp_document('26 Apr 2024 00:00:00 GMT'),url)
        prn_claim={'artist':'Different Act','album':'Different Record','year':2026,'release_date':'2026-10-02','styles':['Post-rock'],'source_ids':['postrocknation:50'],'source_urls':[BASE+'/albums/50/different-record']}
        return m,url,[community,native,prn_claim]

    def test_wrong_community_url_does_not_merge_different_artists(self):
        m,url,facts=self.facts()
        clean,existing=m.reconcile_bandcamp_references(facts,[])
        rows=m.merge_catalog(clean,existing)
        self.assertEqual({r['artist'] for r in rows},{'Example Act','Different Act'})
        different=next(r for r in rows if r['artist']=='Different Act')
        self.assertNotIn('bandcamp-url:'+url,different['source_ids'])
        self.assertNotIn(url,different['source_urls'])

    def test_fully_refreshed_legacy_false_merge_is_split_without_losing_the_original_id(self):
        m,url,facts=self.facts()
        # The initial saved ID came from the community title before the bad link
        # changed the preferred artist/title to the unrelated primary release.
        old=m.merge_catalog(facts,m.merge_catalog([facts[0],facts[2]]))
        self.assertEqual(len(old),1)
        clean,existing=m.reconcile_bandcamp_references(facts,old)
        rows=m.merge_catalog(clean,existing)
        self.assertEqual(len(rows),2)
        primary=next(r for r in rows if r['artist']=='Example Act')
        self.assertEqual(primary['id'],old[0]['id'])
        self.assertEqual(primary['release_date'],'2024-04-26')
        different=next(r for r in rows if r['artist']=='Different Act')
        self.assertEqual(different['release_date'],'2026-10-02')
        self.assertEqual(len({r['id'] for r in rows}),2)
        self.assertEqual(m.merge_catalog(clean,rows),rows)

    def test_explicit_canonical_artist_alias_still_allows_the_shared_album_link(self):
        m,url,facts=self.facts()
        corrections=[{'artist':'Different Act','album':'Different Record','canonical_artist':'Example Act'}]
        clean,existing=m.reconcile_bandcamp_references(facts,[],corrections)
        self.assertEqual(len(m.merge_catalog(clean,existing,corrections)),1)

    def test_title_only_correction_does_not_approve_a_different_artist_link(self):
        m,url,facts=self.facts()
        corrections=[{'artist':'Different Act','album':'Different Record','canonical_album':'Corrected Different Record'}]
        clean,existing=m.reconcile_bandcamp_references(facts,[],corrections)
        self.assertEqual(len(m.merge_catalog(clean,existing,corrections)),2)

    def test_unattributed_old_source_url_prevents_an_unsafe_legacy_rebuild(self):
        m,url,facts=self.facts()
        old=m.merge_catalog(facts)
        old[0]['source_urls'].append('https://example.com/old-announcement')
        clean,existing=m.reconcile_bandcamp_references(facts,old)
        self.assertEqual(existing,old)

    def test_manual_type_correction_does_not_become_a_raw_source_exclusion(self):
        m,url,facts=self.facts()
        corrections=[{'artist':'Different Act','album':'Different Record','release_type':'ep'}]
        clean,_=m.reconcile_bandcamp_references(facts,[],corrections)
        self.assertTrue(all(row['release_type']=='album' for row in clean))
        self.assertEqual(m.retained_exclusions([],clean),[])

    def test_incomplete_refresh_does_not_rebuild_old_unattributed_evidence(self):
        m,url,facts=self.facts()
        old=m.merge_catalog(facts)
        old[0]['source_ids'].append('discogs-master:999')
        clean,existing=m.reconcile_bandcamp_references(facts,old)
        self.assertEqual(existing,old)


if __name__=='__main__':unittest.main()
