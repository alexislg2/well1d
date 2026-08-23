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
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault('SECRET_KEY', 'clef-de-test')
os.environ.setdefault('COOKIE_SECURE', '0')
os.environ.setdefault('AGENT_HMAC_KEY', 'a' * 64)

PASSWORD = 'motdepasse-de-test'
os.environ['WATERING_PASSWORD_SHA256'] = hashlib.sha256(PASSWORD.encode()).hexdigest()

_tmpdir = tempfile.TemporaryDirectory()
os.environ['WATERING_DB'] = os.path.join(_tmpdir.name, 'watering.db')

import server  # noqa: E402
import wellsig  # noqa: E402  (l'import doit suivre la configuration d'environnement)
import watering  # noqa: E402


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
                run_id = watering.reserve_run(conn, now, 300, '10.0.0.1')
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

    def test_depot_signe_accepte(self):
        server.UPLOAD_REQUIRE_SIGNATURE = True
        body, headers = self.signed({'timestamp': 1780000000, 'height_mm': 2900})
        self.assertEqual(self.post(body, headers).status_code, 200)

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


if __name__ == '__main__':
    unittest.main()
