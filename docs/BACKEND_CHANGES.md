# Backend changes required (Snapfit v3)

Written 2026-10-30. Scope: calorie **intake** only (no burn/TDEE), India-first. The app is being
redesigned (app repo `.product/REDESIGN_V3.md`); this lists what the backend must change to serve it.
The new app should be built against the API *after* these changes, not the current one.

## P0 — accuracy

1. **Indian dish database (IFCT 2017 + INDB).**
   - Load per-100 g values for common Indian dishes into a `foods` collection, with aliases (Hindi,
     Telugu and English spellings: "chapati/roti/phulka") and household units
     (katori ≈ 150 g, roti ≈ 40 g, idli ≈ 40 g, dosa ≈ 80 g, tbsp, tsp).
   - Check the INDB licence before importing; IFCT 2017 is published by NIN.
   - `GET /api/food/search?q=` → fuzzy match on name + aliases, returns per_100g and units.
2. **Ground the scan in that database.**
   - After `analyze_food`, match each item's name to a DB dish. If matched, recompute macros as
     DB per-gram × the model's grams, plus the model's `added_fat_g` oil.
   - Keep the model's own macros when there's no match.
   - Gate with `scripts/eval_accuracy.py`: it must beat the current single call (≈103–109 kcal MAE on
     Nutrition5k) and the weighed Indian set (item 6).
3. **Replace `/estimate-item`'s old chain.** It still uses OpenFoodFacts fuzzy search, then a text-only
   LLM call. That benchmarked at 163 kcal MAE, against ~105 for the scan. Point it at the dish DB, with
   the LLM only as fallback. Today the app calls it when the user renames an item, changes its cooking
   method, or adds an item by hand.
4. **Cooking-method change as maths, not a re-price.** The scan now returns `added_fat_g`, so a change
   like fried → steamed should only scale the oil part, the same way home/restaurant and the oil
   less/normal/more follow-up already work in the app. Move these factors server-side so all clients
   agree: restaurant 1.5×, oil less 0.5×, more 1.5×.
5. **Scale-first logging.**
   - `POST /api/food/analyze` already reads `scale_weight_g` from a visible scale.
   - Accept an optional `net_weight_g` form field (weight typed in, or scale minus tare). When given,
     scale the items to that total so weight is exact and only density comes from the model.
6. **Weighed Indian eval set.**
   - Format: `images/` + `truth.jsonl` (same as n5k), built from ~20 of the owner's weighed home meals.
   - Pick the production model from that set, not just Nutrition5k (US cafeteria food).
   - Candidates and n5k scores so far: gemini-2.5-flash 103–109 kcal MAE at $0.51/1k scans,
     gemini-4-argon 98.5, gpt-6.1-terra 96.
7. **Accuracy metric: % of scans edited.**
   - `_capture_corrections` needs `ai_original` on saved items, but the app never sends it, so
     correction learning and `_personalize` are effectively dead.
   - Either the new app sends `ai_original`, or `/analyze` stores its raw output keyed by
     `image_url` and `/logs` diffs against it server-side (simpler; preferred).

## Remove (decided in the redesign brief: remove, not hide)

8. **Gym.**
   - Delete `routers/gym.py` (`/api/gym-sessions`) and the gym streaks in `services/streak_calc.py`
     (`calculate_weekly_gym_streak`, `consecutive_gym_days_with_skip`).
   - Remove the `gym` field from `daily_checkins` and `gym_tracking` from `FeatureFlags` (added in #41;
     moot once gym goes).
   - Remove the `gym_streak_min_days_per_week` profile field.
9. **Burn / TDEE.**
   - Delete `routers/tdee.py` (`/api/tdee/insight`, `/apply`), `services/adaptive_tdee.py` and
     `routers/body_analysis.py` if it only serves burn.
   - Strip gym/burn fields from `routers/summary.py` and `routers/weight_photos.py`.
10. **Profile goals.**
    - Drop the gym-day/rest-day split (`gym_goal_kcal`, `rest_goal_kcal`, `gym_protein_g`,
      `rest_protein_g`; `routers/insights.py` still falls back to them).
    - Keep one `goal_kcal` + macros. `services/tdee.py` stays only to suggest a starting goal at
      onboarding, with no activity term from gym days.
11. **BYO OpenRouter key.** `routers/settings.py` (`/api/settings/api-key`) → keep behind a dev
    flag or delete; the server key is the default.
12. **Health Connect sync** (`routers/health_sync.py`): only keep it if the new app still writes
    nutrition to Health Connect.

Migration: data-only fields can stay in Mongo, but stop reading them. Don't drop collections without
a backup check (nightly backup at 03:30 UTC).

## Add (to serve the redesign)

13. **One logging streak.** `GET /api/streaks` → `{current, best}` for food-logging days only.
14. **Slim check-in.** `/api/daily-checkin` → weight + sleep (+ optional water) only, plus supplements
    when supplement tracking is on. It currently also carries gym and IF.
15. **Demo scan before signup.** An unauthenticated `POST /api/food/analyze-demo`:
    - Rate-limited per IP/device (e.g. 3/day).
    - No MinIO upload, no log write.
    - Same response shape as `/analyze`.
16. **Today in one call.** `GET /api/summary/today?date=`: totals, goal remaining, meals by slot,
    weight/sleep/supplement status. The new Today tab needs one request, not 5.
17. **Before/after share card.** `POST /api/body-photos/compare` exists; add a server-rendered,
    watermarked image (`GET /api/body-photos/compare/{id}/card.jpg`) for WhatsApp/Instagram sharing.
18. **Weekly insight, food-only.** `services/weekly_insights.py` prompt: intake, protein, logging
    consistency and weight trend vs logged kcal. No burn.
19. **Localised food names.** Dish DB aliases (item 1) let search and scan results show Hindi/Telugu
    names. Add an optional `lang` on `/analyze` and `/describe`, so item names come back in the
    user's language with English kept for matching.

## Already done (2026-10-30)

- Single-call scan (items carry grams + macros): 163 → ~105 kcal MAE on Nutrition5k.
- Scan returns `added_fat_g` and `pieces` per item (#42).
- Barcode lookup with nutrition-label fallback (#40).
- Plain-text OTP email; body photos filter by `days`.
