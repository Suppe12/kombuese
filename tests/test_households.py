"""Households: invitations, people and preferences, number of people, the household match,
several households per installation with separate data, API tokens and MCP per household, backup of all households."""
import json
import unittest

try:
    from .harness import ADMIN, Server, ServerTestCase
    from .test_backup import members as tar_members
except ImportError:
    from harness import ADMIN, Server, ServerTestCase
    from test_backup import members as tar_members

PW = "einladung-passwort-1"


def card(n, meal="haupt"):
    return {"url": f"https://example.invalid/rezept-{n}", "title": f"Gericht {n}", "meal": meal, "tags": ["test"]}


class InviteTest(ServerTestCase):

    def invite(self, client=None, **body):
        d = self.ok((client or self.admin).post("/api/invites", body))
        self.assertRegex(d["code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertTrue(d["link"].endswith("/login?invite=" + d["code"]))
        return d

    def test_single_household_defaults(self):
        me = self.ok(self.admin.get("/api/me"))
        self.assertEqual(me["household"]["id"], 1)
        self.assertTrue(me["instance_admin"])
        self.assertEqual(me["households"], 1)
        h = self.ok(self.admin.get("/api/household"))
        self.assertEqual((h["id"], h["people"]), (1, None))
        self.assertIn(ADMIN[0], [m["username"] for m in h["members"]])
        self.assertEqual(h["people_count"], len(h["members"]), "without a setting: one serving per member")

    def test_invite_redeem_once(self):
        inv = self.invite(note="Anna")
        guest = self.server.client()
        d = self.ok(guest.get("/auth/invite?code=" + inv["code"].lower().replace("-", "")))  # case and dashes do not matter
        self.assertEqual((d["household_id"], d["role"]), (1, "member"))
        self.assertEqual(guest.post("/auth/invite", {"code": inv["code"], "username": "anna", "password": "kurz"}).status, 400)
        self.ok(guest.post("/auth/invite", {"code": inv["code"], "username": "anna", "password": PW, "display_name": "Anna"}))
        me = self.ok(guest.get("/api/me"))
        self.assertEqual((me["username"], me["display_name"], me["role"], me["household"]["id"]), ("anna", "Anna", "member", 1))
        self.assertFalse(me["instance_admin"])
        # single use
        other = self.server.client()
        self.assertEqual(other.get("/auth/invite?code=" + inv["code"]).status, 404)
        self.assertEqual(other.post("/auth/invite", {"code": inv["code"], "username": "zweite", "password": PW}).status, 404)
        listed = {i["id"]: i for i in self.ok(self.admin.get("/api/invites"))}
        self.assertEqual((listed[inv["id"]]["state"], listed[inv["id"]]["used_by"]), ("used", "Anna"))
        self.assertNotIn("code", listed[inv["id"]])
        # members cannot invite, see or revoke invitations
        self.assertEqual(guest.post("/api/invites", {}).status, 403)
        self.assertEqual(guest.get("/api/invites").status, 403)

    def test_revoke_and_admin_role(self):
        inv = self.invite(role="admin", days=2)
        self.ok(self.admin.post(f"/api/invites/{inv['id']}/revoke"))
        self.assertEqual(self.server.client().get("/auth/invite?code=" + inv["code"]).status, 404)
        self.assertEqual(self.admin.post(f"/api/invites/{inv['id']}/revoke").status, 404)
        self.assertEqual(self.admin.post("/api/invites", {"role": "chef"}).status, 400)
        inv = self.invite(role="admin")
        c = self.server.client()
        self.ok(c.post("/auth/invite", {"code": inv["code"], "username": "zweitadmin", "password": PW}))
        self.assertEqual(self.ok(c.get("/api/me"))["role"], "admin")
        self.assertEqual(self.server.client().get("/auth/invite?code=FALSCH-FALSCH").status, 404)

    def test_old_way_with_start_password_stays(self):
        c, uid = self.member()
        self.assertIn(uid, [u["id"] for u in self.ok(self.admin.get("/api/users"))])
        self.assertEqual(self.ok(c.get("/api/me"))["household"]["id"], 1)


class PeopleTest(ServerTestCase):

    def test_prefs_and_people_count(self):
        m, mid = self.member("mia")
        me = self.ok(m.get("/api/me"))
        self.assertEqual(me["prefs"], {"diet": "", "no_spicy": False, "dislikes": [], "notes": ""})
        d = self.ok(m.post(f"/api/members/{mid}", {"display_name": "Mia", "diet": "vegetarian", "no_spicy": True,
                                                    "dislikes": "Koriander, Oliven, koriander", "notes": "mag Suppen"}))
        self.assertEqual((d["diet"], d["no_spicy"], d["dislikes"]), ("vegetarian", True, ["Koriander", "Oliven"]))
        self.assertEqual(m.post(f"/api/members/{mid}", {"diet": "frutarier"}).status, 400)
        self.assertEqual(m.post("/api/members/1", {"diet": "vegan"}).status, 403, "members change only their own")
        self.ok(self.admin.post(f"/api/members/{mid}", {"notes": "isst gern Reis"}), )
        h = self.ok(m.get("/api/household"))
        mia = next(x for x in h["members"] if x["id"] == mid)
        self.assertEqual((mia["name"], mia["prefs"]["notes"], mia["prefs"]["diet"]), ("Mia", "isst gern Reis", "vegetarian"))
        self.assertEqual(h["people_count"], 2)
        # children counted: the number is set by an admin, empty = number of members again
        self.assertEqual(self.ok(self.admin.post("/api/household", {"people": 4}))["people_count"], 4)
        self.assertEqual(m.post("/api/household", {"people": 9}).status, 403)
        self.assertEqual(self.ok(self.admin.get("/api/household"))["people"], 4)
        self.assertEqual(self.ok(self.admin.post("/api/household", {"people": None}))["people_count"], 2)

        # the AI prompts (here via MCP) get the people and their preferences
        tok = self.token()["token"]
        mcp = self.server.client(token=tok)
        r = self.ok(mcp.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                                      "params": {"name": "week", "arguments": {"start": "2030-01-07", "people": "5"}}}))
        text = r["result"]["messages"][0]["content"]["text"]
        self.assertIn("5 people for this plan", text)
        self.assertIn("People for this plan: 5", text)
        self.assertIn("Mia: vegetarian; no spicy food; dislikes Koriander, Oliven; notes: isst gern Reis", text)
        r = self.ok(mcp.post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "refill", "arguments": {}}}))
        self.assertIn("2 people; plan servings for 2", r["result"]["messages"][0]["content"]["text"])
        r = self.ok(mcp.post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                                      "params": {"name": "week", "arguments": {"people": "99"}}}))
        self.assertEqual(r["error"]["code"], -32602)

    def test_week_plan_for_guests(self):
        slug = self.recipe("Gästechili", portionen=4, zutaten=[{"menge": 500, "einheit": "g", "name": "Bohnen"}])
        st = self.ok(self.admin.post("/api/wochenplan", {"start": "2030-01-07", "personen": 3}))
        self.assertEqual(st["personen"], 3)
        tok = self.token()["token"]
        w = self.server.client(token=tok)
        self.ok(w.post("/api/wochenplan/vorschlag", {"start": "2030-01-07", "eintraege": [
            {"datum": "2030-01-07", "typ": "dinner", "slug": slug, "scale": 0.75, "einkaufen": True},
            {"datum": "2030-01-08", "typ": "dinner", "titel": "Rest: Gästechili"}]}))
        v = self.ok(self.admin.get("/api/wochenplan"))["vorschlag"]
        self.assertEqual(v["personen"], 3)
        v = self.ok(self.admin.post("/api/wochenplan/personen", {"personen": 6}))["vorschlag"]
        self.assertEqual((v["personen"], v["eintraege"][0]["scale"]), (6, 1.5))
        self.assertEqual(self.admin.post("/api/wochenplan/personen", {"personen": 0}).status, 400)
        self.ok(self.admin.post("/api/wochenplan/verwerfen"))
        self.assertEqual(self.admin.post("/api/wochenplan/personen", {"personen": 2}).status, 409)


class MatchTest(ServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tok = cls.admin.post("/api/tokens", {"name": "karten", "scope": "api"}).json()["token"]

    def cards(self, *ns):
        d = self.ok(self.server.client(token=self.tok).post("/api/cards", [card(n) for n in ns]))
        return d["added"]

    def stack(self, c):
        return [x["id"] for x in self.ok(c.get("/api/stack?limit=100"))]

    def status_of(self, cid):
        likes = {x["id"]: x for x in self.ok(self.admin.get("/api/likes"))}
        if cid in likes:
            return "like", likes[cid]["plan"]
        nopes = {x["id"] for x in self.ok(self.admin.get("/api/nopes?limit=200"))}
        return ("nope", 0) if cid in nopes else ("neu", 0)

    def test_alone_unchanged(self):
        srv = Server().start()  # other tests of this class add members, so a server of its own
        try:
            c = srv.client()
            self.ok(c.post("/auth/setup", {"username": ADMIN[0], "password": ADMIN[1]}))
            tok = self.ok(c.post("/api/tokens", {"name": "t", "scope": "api"}))["token"]
            cid = self.ok(srv.client(token=tok).post("/api/cards", [card(1)]))["added"][0]
            self.assertEqual(self.ok(c.post("/api/swipe", {"id": cid, "action": "like"})), {"ok": True})
            self.assertEqual([x["id"] for x in self.ok(c.get("/api/likes?pending=1"))], [cid])
            self.assertNotIn("match", self.ok(c.get("/api/status")))
            self.assertEqual(self.ok(c.get("/api/match"))["waiting"], [])
        finally:
            srv.stop()

    def test_match_needs_everybody(self):
        m, mid = self.member("partner")
        self.ok(m.post(f"/api/members/{mid}", {"display_name": "Partner"}))
        a, b, c, d = self.cards("a", "b", "c", "d")
        r = self.ok(self.admin.post("/api/swipe", {"id": a, "action": "like"}))
        self.assertEqual((r["match"], r["decided"], r["waiting"], r["members"]), (False, False, ["Partner"], 2))
        self.assertEqual(self.admin.post("/api/swipe", {"id": a, "action": "like"}).status, 409)
        self.assertEqual(self.status_of(a), ("neu", 0))
        self.assertNotIn(a, self.stack(self.admin))
        self.assertIn(a, self.stack(m))
        st = self.ok(self.admin.get("/api/status"))
        self.assertEqual(st["match"]["waiting"], 1)
        w = self.ok(self.admin.get("/api/match"))["waiting"]
        self.assertEqual([(x["id"], x["waiting"], x["title"]) for x in w], [(a, ["Partner"], "Gericht a")])
        # the import job (worker, token) sees nothing yet
        self.assertEqual(self.ok(self.server.client(token=self.tok).get("/api/likes?pending=1")), [])
        r = self.ok(m.post("/api/swipe", {"id": a, "action": "like"}))
        self.assertEqual((r["match"], r["waiting"]), (True, []))
        self.assertEqual(self.status_of(a), ("like", 0))
        self.assertEqual([x["id"] for x in self.ok(self.server.client(token=self.tok).get("/api/likes?pending=1"))], [a])
        # one no ends it
        self.ok(self.admin.post("/api/swipe", {"id": b, "action": "like"}))
        r = self.ok(m.post("/api/swipe", {"id": b, "action": "nope"}))
        self.assertEqual((r["match"], r["decided"], r["status"]), (False, True, "nope"))
        self.assertEqual(self.status_of(b)[0], "nope")
        self.ok(m.post(f"/api/cards/{b}/why", {"why": ["scharf"]}))
        # ★ counts as like and plans it
        self.ok(self.admin.post("/api/swipe", {"id": c, "action": "plan"}))
        self.ok(m.post("/api/swipe", {"id": c, "action": "like"}))
        self.assertEqual(self.status_of(c), ("like", 1))
        # undo takes back only my vote
        self.ok(self.admin.post("/api/swipe", {"id": d, "action": "like"}))
        self.ok(self.admin.post("/api/undo", {"id": d}))
        self.assertIn(d, self.stack(self.admin))
        self.assertEqual(self.ok(self.admin.get("/api/match"))["waiting"], [])
        # when a member leaves, cards that only waited for them are decided
        e, = self.cards("e")
        self.ok(self.admin.post("/api/swipe", {"id": e, "action": "like"}))
        self.ok(self.admin.post(f"/api/users/{mid}", {"delete": True}))
        self.assertEqual(self.status_of(e)[0], "like")


class TwoHouseholdsTest(ServerTestCase):

    def second(self, name="Ferienhaus"):
        d = self.ok(self.admin.post("/api/households", {"name": name, "language": "en"}))
        c = self.server.client()
        self.ok(c.post("/auth/invite", {"code": d["invite"]["code"], "username": f"chef{d['id']}", "password": PW}))
        return d["id"], c

    def test_separate_data(self):
        hid, other = self.second()
        me = self.ok(other.get("/api/me"))
        self.assertEqual((me["household"], me["role"], me["instance_admin"]), ({"id": hid, "name": "Ferienhaus"}, "admin", False))
        self.recipe("Nur im ersten")
        self.ok(other.post("/api/rezepte", {"name": "Nur im zweiten"}))
        self.assertEqual([r["name"] for r in self.ok(other.get("/api/rezepte"))], ["Nur im zweiten"])
        self.assertNotIn("Nur im zweiten", [r["name"] for r in self.ok(self.admin.get("/api/rezepte"))])
        self.assertEqual(other.get("/api/rezepte/nur-im-ersten").status, 404)
        self.ok(other.post("/api/einkauf/neu", {"text": "Kaffee"}))
        self.assertNotIn("Kaffee", [i["name"] for i in self.ok(self.admin.get("/api/einkauf"))["items"]])
        self.assertEqual(self.ok(other.get("/api/household"))["language"], "en")
        # accounts, tokens and invitations only of the own household
        self.assertEqual([u["username"] for u in self.ok(other.get("/api/users"))], [f"chef{hid}"])
        self.assertNotIn(f"chef{hid}", [u["username"] for u in self.ok(self.admin.get("/api/users"))])
        tok = self.ok(other.post("/api/tokens", {"name": "zweit", "scope": "api"}))
        self.assertEqual([r["name"] for r in self.ok(self.server.client(token=tok["token"]).get("/api/rezepte"))], ["Nur im zweiten"])
        self.assertNotIn(tok["id"], [t["id"] for t in self.ok(self.admin.get("/api/tokens"))])
        self.assertEqual(self.admin.post(f"/api/tokens/{tok['id']}/revoke").status, 404)
        self.assertEqual(self.admin.post("/api/users/" + str(self.ok(other.get("/api/me"))["id"]), {"role": "member"}).status, 404)
        # the last admin of a household stays
        self.assertEqual(other.post(f"/api/users/{me['id']}", {"role": "member"}).status, 400)
        # only instance admins manage households and backups
        for path in ("/api/households", "/api/backup"):
            self.assertEqual(other.get(path).status, 403)
        self.assertEqual(other.post("/api/households", {"name": "x"}).status, 403)
        hh = {h["id"]: h for h in self.ok(self.admin.get("/api/households"))}
        self.assertEqual((hh[hid]["name"], hh[hid]["members"], hh[1]["members"]), ("Ferienhaus", 1, 1))
        self.assertEqual(self.admin.post(f"/api/households/{hid}", {"delete": True}).status, 409)
        self.assertEqual(self.admin.post("/api/households/1", {"delete": True}).status, 400)
        self.assertEqual(self.ok(self.admin.post(f"/api/households/{hid}", {"name": "Hütte"}))["name"], "Hütte")
        inv = self.ok(self.admin.post(f"/api/households/{hid}/invites", {"role": "member"}))
        self.assertEqual(self.ok(self.server.client().get("/auth/invite?code=" + inv["code"]))["household"], "Hütte")

    def test_empty_household_can_be_deleted(self):
        d = self.ok(self.admin.post("/api/households", {"name": "Irrtum"}))
        self.ok(self.admin.post(f"/api/households/{d['id']}", {"delete": True}))
        self.assertEqual(self.server.client().get("/auth/invite?code=" + d["invite"]["code"]).status, 404)
        self.assertNotIn(d["id"], [h["id"] for h in self.ok(self.admin.get("/api/households"))])
        self.assertEqual(self.admin.post("/api/households", {"name": " "}).status, 400)

    def test_backup_keeps_all_households(self):
        hid, other = self.second("Zweitwohnung")
        self.ok(other.post("/api/rezepte", {"name": "Zweitrezept"}))
        self.recipe("Erstrezept")
        r = self.admin.get("/api/backup")
        files = tar_members(self.ok(r).body)
        self.assertIn(f"households/{hid}/food-tinder.db", files)
        man = json.loads(files["manifest.json"])
        self.assertEqual(man["households"][str(hid)]["counts"]["recipes"], 1)
        new = Server().start()
        try:
            guest = new.client()
            self.ok(guest.post("/auth/setup/import", raw=r.body, content_type="application/gzip"))
            c = new.client()
            c.login(f"chef{hid}", PW)
            self.assertEqual([x["name"] for x in self.ok(c.get("/api/rezepte"))], ["Zweitrezept"])
            a = new.client()
            a.login(*ADMIN)
            self.assertIn("Erstrezept", [x["name"] for x in self.ok(a.get("/api/rezepte"))])
            self.assertNotIn("Zweitrezept", [x["name"] for x in self.ok(a.get("/api/rezepte"))])
        finally:
            new.stop()


if __name__ == "__main__":
    unittest.main()
