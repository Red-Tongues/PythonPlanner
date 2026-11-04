# Pantry Pal — planning dump (v0.3?? check w/ Priya)

Pantry Pal is a small web app that tracks what's in the kitchen cupboard, warns before food
expires and suggests meals from what is left. Target: shared student houses. MVP by end of term.

notes from kickoff call: keep it cheap, no native app yet, must work offline-ish on phones

## Inventory

We need a reliable list of what is in the pantry.

- Barcode scanning via phone camera (use an open food database, NOT a paid API)
- manual add/edit/delete of items with quantity + expiry date
  - expiry date optional for tins
  - bulk add from a pasted shopping receipt?? (stretch)
- Shared household inventory — everyone in the house sees the same list

TODO: decide whether quantities are counts or weights (both??)

## Expiry alerts
### Daily digest email
Send one email per household each morning listing items expiring in the next 3 days.
Must be skippable per user.

### Push notifications
Only if the PWA route works. Otherwise drop.

**Meal suggestions**

1. Suggest 3 recipes that use the items expiring soonest
2. Filter by dietary tags (vegetarian, vegan, halal, nut-free)
3. "Cook it" button removes used ingredients from the inventory

Note: Sam thinks recipe data licensing could be a problem — check before we scrape anything.

## Accounts & households
* sign up / log in (magic link, no passwords)
* invite housemates by link
* leave a household / transfer ownership

## Random ideas (not committed)

Gamify waste reduction? Leaderboard per house. Probably v2.

## Tech / infra
- Python backend (FastAPI), Postgres, simple HTML front end
- CI with tests + lint on every PR
- [ ] write the deployment runbook
- FIXME the staging DB still has real emails in it — scrub before demo
