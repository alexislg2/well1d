#!/usr/bin/env python3
"""
Tests de la logique d'arrosage : auth, anti-rejeu, verrou d'unicité, sweep.

    python -m unittest test_watering -v

L'agent raspberry est simulé : aucun réseau, aucune vanne.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault('SECRET_KEY', 'clef-de-test')
os.environ.setdefault('COOKIE_SECURE', '0')
os.environ.setdefault('AGENT_HMAC_KEY', 'a' * 64)

PASSWORD = 'motdepasse-de-test'
os.environ['WATERING_PASSWORD_SHA256'] = hashlib.sha256(PASSWORD.encode()).hexdigest()

# Bases jetables : sans ça les tests d'upload écrivaient leurs mesures bidon
# dans la copie de développement de well.db.
_tmpdir = tempfile.TemporaryDirectory()
os.environ['WATERING_DB'] = os.path.join(_tmpdir.name, 'watering.db')
os.environ['WELL_DB'] = os.path.join(_tmpdir.name, 'well.db')

import server  # noqa: E402  (les imports doivent suivre la configuration d'environnement)
import watering  # noqa: E402
import wellsig  # noqa: E402

server.create_database()


def proof_for(nonce, action, params):
    key = bytes.fromhex(os.environ['WATERING_PASSWORD_SHA256'])
    message = '{}|{}|{}'.format(nonce, action, params).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def agent_ok(path, payload=None):
    return True, {'ok': True, 'ret': 0, 'is_watering': path == '/start'}, None


def agent_idle(path, payload=None):
    return True, {'ok': True, 'ret': 0, 'is_watering': False}, None


def agent_down(path, payload=None):
    return False, None, "agent injoignable"


class WateringTestCase(unittest.TestCase):
    def setUp(self):
        server.app.config['TESTING'] = True
        self.client = server.app.test_client()

        # Aucun test ne doit sortir sur le réseau : l'agent raspberry est simulé
        # partout, y compris dans les sondages implicites de build_state().
        patcher = mock.patch.object(watering, 'agent_call',
                                    lambda path, payload=None: agent_idle(path, payload))
        patcher.start()
        self.addCleanup(patcher.stop)

        conn = watering.connect()
        for table in ('watering_run', 'used_nonce', 'auth_failure'):
            conn.execute("DELETE FROM {}".format(table))
        conn.execute("UPDATE gateway_state SET polled_at=0, reachable=0, is_watering=NULL")
        conn.close()

    def challenge(self, action):
        response = self.client.get('/watering/challenge?action=' + action)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['nonce']

    def login(self):
        nonce = self.challenge('login')
        return self.client.post('/watering/login',
                                json={'nonce': nonce, 'proof': proof_for(nonce, 'login', '')})

    def start(self, minutes=5, agent=agent_ok):
        nonce = self.challenge('start')
        with mock.patch.object(watering, 'agent_call', agent):
            return self.client.post('/watering/start', json={
                'nonce': nonce,
                'proof': proof_for(nonce, 'start', str(minutes)),
                'minutes': minutes,
            })

    def runs(self):
        conn = watering.connect()
        try:
            return conn.execute("SELECT * FROM watering_run ORDER BY id").fetchall()
        finally:
            conn.close()


class TestAuth(WateringTestCase):
    def test_bon_mot_de_passe(self):
        self.assertEqual(self.login().status_code, 200)

    def test_mauvais_mot_de_passe_enregistre_un_echec(self):
        nonce = self.challenge('login')
        response = self.client.post('/watering/login', json={'nonce': nonce, 'proof': 'ff' * 32})
        self.assertEqual(response.status_code, 401)

        conn = watering.connect()
        count, = conn.execute("SELECT COUNT(*) FROM auth_failure").fetchone()
        conn.close()
        self.assertEqual(count, 1)

    def test_nonce_a_usage_unique(self):
        nonce = self.challenge('login')
        body = {'nonce': nonce, 'proof': proof_for(nonce, 'login', '')}
        self.assertEqual(self.client.post('/watering/login', json=body).status_code, 200)

        replay = self.client.post('/watering/login', json=body)
        self.assertEqual(replay.status_code, 401)
        self.assertIn('déjà utilisé', replay.get_json()['error'])

    def test_nonce_lie_a_son_action(self):
        self.login()
        nonce = self.challenge('stop')
        response = self.client.post('/watering/start', json={
            'nonce': nonce, 'proof': proof_for(nonce, 'start', '5'), 'minutes': 5})
        self.assertEqual(response.status_code, 401)
        self.assertIn('autre action', response.get_json()['error'])

    def test_duree_falsifiee_rejetee(self):
        self.login()
        nonce = self.challenge('start')
        # Preuve calculée pour 5 minutes, corps annonçant 120 minutes.
        response = self.client.post('/watering/start', json={
            'nonce': nonce, 'proof': proof_for(nonce, 'start', '5'), 'minutes': 120})
        self.assertEqual(response.status_code, 401)

    def test_nonce_expire(self):
        self.login()
        nonce = self.challenge('stop')
        future = time.time() + watering.NONCE_TTL + 10
        with mock.patch.object(watering.time, 'time', return_value=future):
            response = self.client.post('/watering/stop',
                                        json={'nonce': nonce, 'proof': proof_for(nonce, 'stop', '')})
        self.assertEqual(response.status_code, 401)

    def test_force_brute_bloquee(self):
        for _ in range(watering.MAX_AUTH_FAILURES):
            nonce = self.challenge('login')
            self.client.post('/watering/login', json={'nonce': nonce, 'proof': 'ff' * 32})
        nonce = self.challenge('login')
        response = self.client.post('/watering/login',
                                    json={'nonce': nonce, 'proof': proof_for(nonce, 'login', '')})
        self.assertEqual(response.status_code, 429)

    def test_actions_refusees_sans_session(self):
        self.assertEqual(self.client.get('/watering/state').status_code, 401)
        self.assertEqual(self.client.get('/watering/challenge?action=start').status_code, 401)
        self.assertEqual(self.client.post('/watering/start', json={}).status_code, 401)


class TestStart(WateringTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_demarrage_nominal(self):
        response = self.start(5)
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state['current']['status'], 'running')
        self.assertEqual(state['current']['duration_s'], 300)
        self.assertFalse(state['can_start'])

    def test_refus_si_arrosage_en_cours(self):
        self.start(5)
        response = self.start(5)
        self.assertEqual(response.status_code, 409)
        self.assertIn('déjà en cours', response.get_json()['error'])

    def test_duree_hors_limites(self):
        for minutes in (0, -1, watering.MAX_MINUTES + 1):
            self.assertEqual(self.start(minutes).status_code, 400)
        nonce = self.challenge('start')
        response = self.client.post('/watering/start', json={
            'nonce': nonce, 'proof': proof_for(nonce, 'start', 'abc'), 'minutes': 'abc'})
        self.assertEqual(response.status_code, 400)

    def test_echec_agent_libere_le_verrou(self):
        response = self.start(5, agent=agent_down)
        self.assertEqual(response.status_code, 502)

        rows = self.runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertIsNone(rows[0]['active'])

        # Le verrou est libéré : la tentative suivante passe immédiatement.
        self.assertEqual(self.start(5).status_code, 200)

    def test_une_seule_reservation_en_concurrence(self):
        """Reproduit ce que font 16 workers gunicorn sur le même fichier sqlite."""
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)
        now = int(time.time())

        def attempt():
            conn = watering.connect()
            try:
                barrier.wait()
                run_id = watering.reserve_run(conn, now, 300, 'web', '10.0.0.1')
            finally:
                conn.close()
            with lock:
                results.append(run_id)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len([r for r in results if r is not None]), 1)
        conn = watering.connect()
        count, = conn.execute("SELECT COUNT(*) FROM watering_run WHERE active = 1").fetchone()
        conn.close()
        self.assertEqual(count, 1)


class TestStopEtSweep(WateringTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_arret_manuel(self):
        self.start(5)
        nonce = self.challenge('stop')
        with mock.patch.object(watering, 'agent_call', agent_ok):
            response = self.client.post('/watering/stop',
                                        json={'nonce': nonce, 'proof': proof_for(nonce, 'stop', '')})
        self.assertEqual(response.status_code, 200)

        row = self.runs()[0]
        self.assertEqual(row['status'], 'stopped')
        self.assertEqual(row['stop_reason'], 'manual')
        self.assertIsNone(row['active'])

    def test_arret_non_confirme_laisse_le_run_ouvert(self):
        self.start(5)
        nonce = self.challenge('stop')
        with mock.patch.object(watering, 'agent_call', agent_down):
            response = self.client.post('/watering/stop',
                                        json={'nonce': nonce, 'proof': proof_for(nonce, 'stop', '')})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.runs()[0]['status'], 'running')

    def test_sweep_cloture_un_run_expire(self):
        self.start(1)
        conn = watering.connect()
        try:
            now = int(time.time())
            conn.execute("UPDATE watering_run SET started_at=?, planned_end=?",
                         (now - 120, now - 60))
            conn.execute("BEGIN IMMEDIATE")
            watering.sweep(conn, now)
            conn.execute("COMMIT")
        finally:
            conn.close()

        row = self.runs()[0]
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['stop_reason'], 'expired')
        self.assertIsNone(row['active'])

    def test_sweep_recupere_une_reservation_orpheline(self):
        now = int(time.time())
        conn = watering.connect()
        try:
            conn.execute(
                "INSERT INTO watering_run (requested_at, duration_s, status, active)"
                " VALUES (?, 300, 'pending', 1)", (now - watering.ORPHAN_TTL - 1,))
            conn.execute("BEGIN IMMEDIATE")
            watering.sweep(conn, now)
            conn.execute("COMMIT")
        finally:
            conn.close()

        row = self.runs()[0]
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['active'])
        self.assertEqual(self.start(5).status_code, 200)


class TestReconciliation(WateringTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def _poll(self, is_watering, now):
        agent = lambda path, payload=None: (True, {'ok': True, 'is_watering': is_watering}, None)
        conn = watering.connect()
        try:
            with mock.patch.object(watering, 'agent_call', agent):
                watering.refresh_gateway(conn, now, max_age=0)
        finally:
            conn.close()

    def test_vanne_fermee_cloture_apres_deux_sondages(self):
        self.start(30)
        conn = watering.connect()
        try:
            conn.execute("UPDATE watering_run SET started_at = started_at - 60 WHERE active = 1")
        finally:
            conn.close()

        now = int(time.time())
        self._poll(True, now)                                  # la vanne a confirmé l'ouverture
        self._poll(False, now + 5)
        self.assertEqual(self.runs()[0]['status'], 'running')  # un seul sondage ne suffit pas

        self._poll(False, now + 10)
        row = self.runs()[0]
        self.assertEqual(row['status'], 'stopped')
        self.assertEqual(row['stop_reason'], 'gateway_off')

    def test_delai_de_grace_apres_le_demarrage(self):
        self.start(30)
        now = int(time.time())
        self._poll(False, now)
        self._poll(False, now + 1)
        # started_at est tout frais : la GW-02 met quelques secondes à suivre.
        self.assertEqual(self.runs()[0]['status'], 'running')

    def test_vanne_ouverte_hors_application(self):
        now = int(time.time())
        self._poll(True, now)
        state = self.client.get('/watering/state').get_json()
        self.assertTrue(state['foreign_watering'])
        self.assertFalse(state['can_start'])
        self.assertTrue(state['can_stop'])
        self.assertIsNone(state['current'])

        self.assertEqual(self.start(5).status_code, 409)


class TestFormatSurLeFil(unittest.TestCase):
    """Le format signé est un contrat entre deux machines déployées
    séparément : le changer casse l'arrosage le temps du déploiement."""

    def test_message_signe_inchange(self):
        key = b'cle-de-test'
        body = b'{"duration_s": 600}'
        headers = wellsig.sign(key, 'POST', '/start', body)
        attendu = hmac.new(key, "\n".join([
            headers['X-Well-Ts'], headers['X-Well-Nonce'], 'POST', '/start',
            hashlib.sha256(body).hexdigest(),
        ]).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Well-Sig'], attendu)

    def test_verify_accepte_ce_que_sign_produit(self):
        key = b'cle-de-test'
        body = b'{}'
        headers = wellsig.sign(key, 'POST', '/status', body)
        self.assertIsNone(wellsig.verify(key, 'POST', '/status', body, headers))
        self.assertIsNotNone(wellsig.verify(b'autre', 'POST', '/status', body, headers))


class TestUploadSigne(WateringTestCase):
    """POST /upload_data : le raspberry signe, le serveur vérifie."""

    def setUp(self):
        super().setUp()
        self.key = os.environ['AGENT_HMAC_KEY'].encode()
        server.UPLOAD_HMAC_KEY = self.key

    def post(self, body, headers=None):
        return self.client.post('/upload_data', data=body,
                                headers=headers or {}, content_type='application/json')

    def signed(self, payload):
        body = json.dumps(payload).encode()
        return body, wellsig.sign(self.key, 'POST', '/upload_data', body)

    def mesures(self, timestamp):
        conn = sqlite3.connect(os.environ['WELL_DB'])
        try:
            return conn.execute("SELECT height_mm FROM water_height WHERE timestamp = ?",
                                (timestamp,)).fetchall()
        finally:
            conn.close()

    def test_depot_signe_accepte(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body, headers = self.signed({'timestamp': 1780000000, 'height_mm': 2900})
        self.assertEqual(self.post(body, headers).status_code, 200)
        self.assertEqual(self.mesures(1780000000), [(2900,)])

    def test_depot_refuse_n_enregistre_rien(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        self.post(json.dumps({'timestamp': 1780000099, 'height_mm': 1}).encode())
        self.assertEqual(self.mesures(1780000099), [])

    def test_depot_non_signe_refuse(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        r = self.post(json.dumps({'timestamp': 1780000000, 'height_mm': 2900}).encode())
        self.assertEqual(r.status_code, 401)
        self.assertIn('signature', r.get_json()['error'])

    def test_depot_non_signe_toleré_pendant_la_migration(self):
        server.UPLOAD_REQUIRE_SIGNATURE = False
        r = self.post(json.dumps({'timestamp': 1780000001, 'height_mm': 2900}).encode())
        self.assertEqual(r.status_code, 200)

    def test_mesure_falsifiee_en_vol(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body, headers = self.signed({'timestamp': 1780000000, 'height_mm': 2900})
        falsifie = json.dumps({'timestamp': 1780000000, 'height_mm': 9999}).encode()
        self.assertEqual(self.post(falsifie, headers).status_code, 401)

    def test_rejeu_refuse(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body, headers = self.signed({'timestamp': 1780000000, 'height_mm': 2900})
        self.assertEqual(self.post(body, headers).status_code, 200)
        r = self.post(body, headers)
        self.assertEqual(r.status_code, 401)
        self.assertIn('déjà utilisé', r.get_json()['error'])

    def test_signature_arrosage_non_rejouable_sur_upload(self):
        """Le chemin fait partie du message signé."""
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body = json.dumps({'timestamp': 1780000000, 'height_mm': 2900}).encode()
        headers = wellsig.sign(self.key, 'POST', '/start', body)
        self.assertEqual(self.post(body, headers).status_code, 401)

    def test_horodatage_hors_tolerance(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body = json.dumps({'timestamp': 1780000000, 'height_mm': 2900}).encode()
        vieux = time.time() - wellsig.MAX_SKEW - 10
        with mock.patch.object(wellsig.time, 'time', return_value=vieux):
            headers = wellsig.sign(self.key, 'POST', '/upload_data', body)
        self.assertEqual(self.post(body, headers).status_code, 401)

    def test_mesure_nulle_acceptee(self):
        """Sonde muette : l'absence de mesure est une information, pas une erreur."""
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body, headers = self.signed({'timestamp': 1780000002, 'height_mm': None})
        self.assertEqual(self.post(body, headers).status_code, 200)

    def test_corps_invalide(self):
        server.UPLOAD_REQUIRE_SIGNATURE = False
        self.assertEqual(self.post(b'{}').status_code, 400)
        self.assertEqual(self.post(b'pas du json').status_code, 400)
        body = json.dumps({'timestamp': 'abc', 'height_mm': 1}).encode()
        self.assertEqual(self.post(body).status_code, 400)


class TestQualiteLiaison(WateringTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_qualification(self):
        self.assertEqual(watering.signal_quality(100), 'bon')
        self.assertEqual(watering.signal_quality(70), 'bon')
        self.assertEqual(watering.signal_quality(69), 'moyen')
        self.assertEqual(watering.signal_quality(40), 'moyen')
        self.assertEqual(watering.signal_quality(36), 'faible')
        self.assertIsNone(watering.signal_quality(None))

    def _sonder(self, **champs):
        reponse = dict({'ok': True, 'is_watering': False}, **champs)
        agent = lambda path, payload=None: (True, reponse, None)
        conn = watering.connect()
        try:
            with mock.patch.object(watering, 'agent_call', agent):
                watering.refresh_gateway(conn, int(time.time()), max_age=0)
        finally:
            conn.close()
        return self.client.get('/watering/state').get_json()['gateway']

    def test_signal_et_pile_remontes(self):
        g = self._sonder(signal=36, battery=87, rf_linked=True)
        self.assertEqual(g['signal'], 36)
        self.assertEqual(g['signal_quality'], 'faible')
        self.assertEqual(g['battery'], 87)
        self.assertTrue(g['rf_linked'])

    def test_vanne_hors_de_portee(self):
        g = self._sonder(signal=0, battery=100, rf_linked=False)
        self.assertFalse(g['rf_linked'])

    def test_passerelle_sans_champ_signal(self):
        """Un firmware qui ne renvoie pas ces champs ne doit rien casser."""
        g = self._sonder()
        self.assertIsNone(g['signal'])
        self.assertIsNone(g['signal_quality'])
        self.assertIsNone(g['rf_linked'])


class TestProgrammation(WateringTestCase):
    def setUp(self):
        super().setUp()
        conn = watering.connect()
        conn.execute("DELETE FROM watering_schedule")
        conn.close()
        self.login()

    def poser(self, retard_s, minutes=2):
        """Programmation dont l'échéance est déjà passée de `retard_s`."""
        now = int(time.time())
        conn = watering.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO watering_schedule (created_at, at_ts, duration_s, status)"
                " VALUES (?, ?, ?, 'pending')", (now, now - retard_s, minutes * 60))
            conn.execute("COMMIT")
            return cur.lastrowid
        finally:
            conn.close()

    def etat(self, sid):
        conn = watering.connect()
        try:
            return conn.execute(
                "SELECT status, reason, run_id FROM watering_schedule WHERE id=?",
                (sid,)).fetchone()
        finally:
            conn.close()

    def programmer(self, at='07:30', minutes=15):
        nonce = self.challenge('schedule')
        return self.client.post('/watering/schedule', json={
            'nonce': nonce,
            'proof': proof_for(nonce, 'schedule', '{}|{}'.format(at, minutes)),
            'at': at, 'minutes': minutes,
        })

    # ---- résolution de l'heure ----

    def test_prochaine_occurrence_aujourdhui_ou_demain(self):
        tz = watering.local_timezone
        midi = int(tz.localize(datetime(2026, 6, 15, 12, 0)).timestamp())
        soir = watering.next_occurrence(20, 0, midi)
        self.assertEqual(datetime.fromtimestamp(soir, tz).strftime('%Y-%m-%d %H:%M'), '2026-06-15 20:00')
        matin = watering.next_occurrence(8, 0, midi)
        self.assertEqual(datetime.fromtimestamp(matin, tz).strftime('%Y-%m-%d %H:%M'), '2026-06-16 08:00')

    def test_heure_deja_passee_de_peu_bascule_a_demain(self):
        tz = watering.local_timezone
        maintenant = int(tz.localize(datetime(2026, 6, 15, 8, 0, 10)).timestamp())
        suivant = watering.next_occurrence(8, 0, maintenant)
        self.assertEqual(datetime.fromtimestamp(suivant, tz).strftime('%Y-%m-%d'), '2026-06-16')

    def test_changement_dheure_ne_decale_pas(self):
        """Le 25/10/2026 la France recule d'une heure à 3 h."""
        tz = watering.local_timezone
        veille = int(tz.localize(datetime(2026, 10, 24, 12, 0)).timestamp())
        cible = watering.next_occurrence(9, 0, veille)
        self.assertEqual(datetime.fromtimestamp(cible, tz).strftime('%Y-%m-%d %H:%M'), '2026-10-25 09:00')

    # ---- création et annulation ----

    def test_programmation_puis_annulation(self):
        state = self.programmer().get_json()
        self.assertEqual(len(state['schedules']), 1)
        sid = state['schedules'][0]['id']
        self.assertEqual(state['schedules'][0]['duration_s'], 900)

        nonce = self.challenge('unschedule')
        r = self.client.post('/watering/unschedule', json={
            'nonce': nonce, 'proof': proof_for(nonce, 'unschedule', str(sid)), 'id': sid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['schedules'], [])

    def test_deux_programmations_a_la_meme_heure(self):
        self.assertEqual(self.programmer('07:30', 15).status_code, 200)
        self.assertEqual(self.programmer('07:30', 20).status_code, 409)

    def test_entrees_invalides(self):
        for at in ('25:00', '7:30', 'midi', ''):
            self.assertEqual(self.programmer(at).status_code, 400, at)
        self.assertEqual(self.programmer('07:30', watering.MAX_MINUTES + 1).status_code, 400)
        self.assertEqual(self.programmer('07:30', 0).status_code, 400)

    def test_annulation_inexistante(self):
        nonce = self.challenge('unschedule')
        r = self.client.post('/watering/unschedule', json={
            'nonce': nonce, 'proof': proof_for(nonce, 'unschedule', '999'), 'id': 999})
        self.assertEqual(r.status_code, 404)

    def test_programmation_refusee_sans_session(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.post('/watering/schedule', json={}).status_code, 401)

    # ---- déclenchement ----

    def test_declenchement_dans_la_fenetre(self):
        sid = self.poser(120)
        with mock.patch.object(watering, 'agent_call', agent_ok):
            lances, manques = watering.fire_due()
        self.assertEqual(lances, [sid])
        row = self.etat(sid)
        self.assertEqual(row['status'], 'fired')
        self.assertIsNotNone(row['run_id'])

        run = self.runs()[0]
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['source'], 'schedule')

    def test_retard_au_dela_de_la_grace(self):
        sid = self.poser(watering.SCHEDULE_GRACE + 60)
        with mock.patch.object(watering, 'agent_call', agent_ok):
            watering.fire_due()
        row = self.etat(sid)
        self.assertEqual(row['status'], 'missed')
        self.assertIn('retard', row['reason'])
        self.assertEqual(self.runs(), [])          # aucun arrosage n'a été créé

    def test_echeance_pendant_un_arrosage(self):
        self.start(5)
        sid = self.poser(60)
        with mock.patch.object(watering, 'agent_call', agent_ok):
            watering.fire_due()
        row = self.etat(sid)
        self.assertEqual(row['status'], 'missed')
        self.assertIn('déjà en cours', row['reason'])

    def test_echec_de_la_vanne_au_declenchement(self):
        sid = self.poser(60)
        with mock.patch.object(watering, 'agent_call', agent_down):
            watering.fire_due()
        row = self.etat(sid)
        self.assertEqual(row['status'], 'missed')
        self.assertIsNotNone(row['run_id'])        # l'échec reste tracé dans l'historique
        self.assertEqual(self.runs()[0]['status'], 'failed')

    def test_echeance_future_ne_part_pas(self):
        self.programmer('07:30', 15)
        with mock.patch.object(watering, 'agent_call', agent_ok):
            self.assertEqual(watering.fire_due(), ([], []))

    def test_manques_visibles_dans_l_etat(self):
        self.poser(watering.SCHEDULE_GRACE + 60)
        with mock.patch.object(watering, 'agent_call', agent_ok):
            watering.fire_due()
        state = self.client.get('/watering/state').get_json()
        self.assertEqual(len(state['missed']), 1)
        self.assertIn('retard', state['missed'][0]['reason'])


def agent_compteurs(is_watering=False, total_s=None, remain_s=0):
    """Agent qui rapporte les compteurs de la vanne — `total_duration` et
    `remain_duration` — dont dépend l'accusé de réception."""
    def call(path, payload=None):
        return True, {'ok': True, 'ret': 0, 'is_watering': is_watering,
                      'total_s': total_s, 'remain_s': remain_s}, None
    return call


class TestAccuseDeReception(WateringTestCase):
    """La passerelle acquitte un cmd 6 qu'elle n'a pas encore délivré à la
    vanne. Sans relecture des compteurs, un arrosage jamais exécuté s'affiche
    « Terminé » — c'est arrivé en production le 24/08/2026."""

    def setUp(self):
        super().setUp()
        self.login()

    def _poll(self, agent, now):
        conn = watering.connect()
        try:
            with mock.patch.object(watering, 'agent_call', agent):
                watering.refresh_gateway(conn, now, max_age=0)
        finally:
            conn.close()

    def _arroser(self, minutes, avant_total, avant_remain=0):
        """Pose l'état de la vanne avant l'ordre, puis lance l'arrosage."""
        now = int(time.time())
        agent = agent_compteurs(False, total_s=avant_total, remain_s=avant_remain)
        self._poll(agent, now)
        self.assertEqual(self.start(minutes, agent=agent).status_code, 200)
        return now

    def _vieillir(self, secondes):
        conn = watering.connect()
        try:
            conn.execute("UPDATE watering_run SET started_at=started_at-?,"
                         " planned_end=planned_end-? WHERE id=(SELECT MAX(id) FROM watering_run)",
                         (secondes, secondes))
        finally:
            conn.close()

    def test_ordre_jamais_delivre(self):
        now = self._arroser(1, avant_total=120)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        # La vanne rapporte toujours l'arrosage précédent : elle n'a rien reçu.
        self._poll(agent_compteurs(False, total_s=120), now + 1)

        row = self.runs()[0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['stop_reason'], 'not_delivered')
        self.assertIsNone(row['confirmed_at'])

    def test_compteurs_valent_accuse_de_reception(self):
        now = self._arroser(1, avant_total=120)
        # total_duration bascule sur notre durée : la vanne a exécuté l'ordre.
        self._poll(agent_compteurs(False, total_s=60), now + 1)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        self._poll(agent_compteurs(False, total_s=60), now + 2)

        row = self.runs()[0]
        self.assertEqual(row['status'], 'done')
        self.assertIsNotNone(row['confirmed_at'])

    def test_meme_duree_qu_avant_n_accuse_personne(self):
        """Deux arrosages d'une minute de suite : les compteurs sont identiques
        avant et après. On ne peut pas trancher — et on n'invente pas."""
        now = self._arroser(1, avant_total=60, avant_remain=0)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        self._poll(agent_compteurs(False, total_s=60, remain_s=0), now + 1)

        row = self.runs()[0]
        self.assertEqual(row['status'], 'unconfirmed')
        self.assertNotEqual(row['stop_reason'], 'not_delivered')

    def test_vanne_vue_coulant_leve_l_ambiguite(self):
        now = self._arroser(1, avant_total=60, avant_remain=0)
        self._poll(agent_compteurs(True, total_s=60, remain_s=45), now + 1)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        self._poll(agent_compteurs(False, total_s=60, remain_s=0), now + 2)

        self.assertEqual(self.runs()[0]['status'], 'done')

    def test_passerelle_sans_compteurs_ne_juge_rien(self):
        """Firmware qui ne rapporte pas total_duration : on garde le
        comportement d'avant plutôt que d'accuser au hasard."""
        now = self._arroser(1, avant_total=None)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        self._poll(agent_idle, now + 1)

        self.assertEqual(self.runs()[0]['status'], 'done')

    def test_delai_de_grace_avant_verdict(self):
        """La vanne dort : un ordre non encore délivré n'est pas un ordre perdu."""
        now = self._arroser(5, avant_total=120)
        self._poll(agent_compteurs(False, total_s=120), now + 30)

        self.assertEqual(self.runs()[0]['status'], 'running')

    def test_verdict_affiche_dans_l_historique(self):
        now = self._arroser(1, avant_total=120)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        self._poll(agent_compteurs(False, total_s=120), now + 1)

        entree = self.client.get('/watering/state').get_json()['history'][0]
        self.assertEqual(entree['stop_reason'], 'not_delivered')
        self.assertIsNone(entree['actual_s'])   # pas de durée pour ce qui n'a pas coulé

    def test_le_cron_fait_tomber_le_verdict(self):
        """Personne ne regarde la page : c'est le cron qui doit trancher."""
        self._arroser(1, avant_total=120)
        self._vieillir(watering.CONFIRM_DEADLINE + 60)
        with mock.patch.object(watering, 'agent_call',
                               agent_compteurs(False, total_s=120)):
            watering.settle_pending()

        self.assertEqual(self.runs()[0]['stop_reason'], 'not_delivered')


class TestVolumeEstime(WateringTestCase):
    """Pas de compteur d'eau : le volume est un produit débit x durée réelle.
    Le débit vient de la chute de niveau de la citerne (voir README)."""

    def _run(self, duree, ecoule, status='done', stop_reason='expired'):
        conn = watering.connect()
        try:
            debut = int(time.time()) - 3600
            conn.execute(
                "INSERT INTO watering_run (requested_at, started_at, ended_at, duration_s,"
                " status, stop_reason, source) VALUES (?, ?, ?, ?, ?, ?, 'web')",
                (debut, debut, None if ecoule is None else debut + ecoule,
                 duree, status, stop_reason))
            return conn.execute("SELECT * FROM watering_run ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()

    def test_volume_suit_la_duree_reelle(self):
        # Arrêté à mi-course : c'est le temps écoulé qui compte, pas le demandé.
        vu = watering.run_to_dict(self._run(600, 300, 'stopped', 'manual'))
        self.assertEqual(vu['actual_s'], 300)
        self.assertEqual(vu['volume_l'], round(5 * watering.LITERS_PER_WATERING_MINUTE))

    def test_pas_de_volume_pour_un_arrosage_jamais_execute(self):
        vu = watering.run_to_dict(self._run(60, 60, 'failed', 'not_delivered'))
        self.assertIsNone(vu['volume_l'])
        self.assertIsNone(vu['actual_s'])

    def test_pas_de_volume_tant_que_l_arrosage_court(self):
        self.assertIsNone(watering.run_to_dict(self._run(600, None, 'running', None))['volume_l'])

    def test_debit_surchargeable(self):
        run = self._run(600, 600)
        with mock.patch.object(watering, 'LITERS_PER_WATERING_MINUTE', 10.0):
            self.assertEqual(watering.run_to_dict(run)['volume_l'], 100)


class TestArrosagesSurLeGraphe(WateringTestCase):
    """Le graphe du niveau lit watering.db pour situer les arrosages : n'y
    figurent que ceux qui ont réellement ouvert la vanne."""

    T0 = 1_700_000_000

    def _run(self, started_at, ended_at=None, planned_end=None, duration_s=600,
             status='done', stop_reason='expired'):
        conn = watering.connect()
        try:
            conn.execute(
                "INSERT INTO watering_run (requested_at, started_at, ended_at, planned_end,"
                " duration_s, status, stop_reason, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'web')",
                (started_at or self.T0, started_at, ended_at, planned_end,
                 duration_s, status, stop_reason))
        finally:
            conn.close()

    def test_arrosage_termine_avec_sa_fenetre_et_son_volume(self):
        self._run(self.T0, self.T0 + 300)
        spans = watering.runs_in_range(self.T0 - 3600, self.T0 + 3600)
        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0]['start'], spans[0]['end']), (self.T0, self.T0 + 300))
        self.assertFalse(spans[0]['running'])
        self.assertEqual(spans[0]['volume_l'], round(5 * watering.LITERS_PER_WATERING_MINUTE))

    def test_arrosage_jamais_delivre_absent(self):
        self._run(self.T0, self.T0 + 60, status='failed', stop_reason='not_delivered')
        self.assertEqual(watering.runs_in_range(self.T0 - 3600, self.T0 + 3600), [])

    def test_reservation_sans_demarrage_absente(self):
        self._run(None, status='failed', stop_reason='agent_error')
        self.assertEqual(watering.runs_in_range(self.T0 - 3600, self.T0 + 3600), [])

    def test_arrosage_en_cours_trace_jusqu_a_la_fin_prevue(self):
        self._run(self.T0, None, planned_end=self.T0 + 600,
                  status='running', stop_reason=None)
        span = watering.runs_in_range(self.T0 - 60, self.T0 + 60)[0]
        self.assertEqual(span['end'], self.T0 + 600)
        self.assertTrue(span['running'])

    def test_fenetre_bornee_mais_chevauchements_gardes(self):
        self._run(self.T0 - 7200, self.T0 - 7000)          # avant
        self._run(self.T0 - 60, self.T0 + 60)              # à cheval sur le début
        self._run(self.T0 + 3600, self.T0 + 7200)          # après
        spans = watering.runs_in_range(self.T0, self.T0 + 1800)
        self.assertEqual([s['start'] for s in spans], [self.T0 - 60])


class TestPageDuGraphe(WateringTestCase):
    def test_le_graphe_embarque_les_arrosages(self):
        server.insert_data(int(time.time()) - 60, 1500)
        page = self.client.get('/').get_data(as_text=True)
        self.assertEqual(page.count('id="show-waterings"'), 1)
        self.assertIn('"waterings"', page)

    def test_le_graphe_survit_a_une_base_arrosage_illisible(self):
        server.insert_data(int(time.time()) - 60, 1500)
        with mock.patch.object(watering, 'runs_in_range', side_effect=sqlite3.OperationalError('bam')):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('"waterings": []', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
