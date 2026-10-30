# Backend changes required (Snapfit v3)

Written 2026-10-30. Scope: calorie **intake** only (no burn/TDEE), India-first. The app is being
redesigned (app repo `.product/REDESIGN_V3.md`); this lists what the backend must change to serve it.
The new app should be built against the API *after* these changes, not the current one.

## P0 — accuracy

1. ~~**Indian dish database (INDB).**~~ **Rejected after testing (2026-10-30).**
   - INDB has no yield factor: "per 100 g" is per 100 g of raw ingredients, not of the cooked dish.
     The paper lists yield correction as future work.
   - Spot checks are off by 30–50% both ways: poha 294 kcal/100 g (cooked is ~130–180), plain dosa
     381 (~165–210), roti 202 (~260–300).
   - Fried recipes count all the frying oil as eaten: samosa 577, besan kadhi with pakodies 403.
   - The repo has no licence file.
   - Name matching against real scan names covered only ~9% of items at a safe threshold.
   - Verdict: grounding scans in it would make numbers worse. A usable Indian DB needs cooked-weight
     values (lab-measured, or recipes with yield factors). Revisit only with such a source; the weighed
     eval set (item 6) is the gate.
2. ~~Ground the scan in that database.~~ Dropped with item 1.
3. **`/estimate-item` still uses OpenFoodFacts fuzzy search, then an LLM.** It's open until there's a
   better source. Item 4 removes most of its callers.
4. **Cooking-method change as maths.** Client-side in the new app: scale only `added_fat_g`, like the
   oil follow-up (restaurant 1.5×, less 0.5×, more 1.5×). No backend change needed.
5. ✅ **Scale-first.** `POST /api/food/analyze` takes `net_weight_g` (scale minus tare); items are scaled
   to that total.
6. **Weighed Indian eval set.** Still needs ~20 of the owner's weighed meals. It picks the production model.
7. ✅ **Accuracy metric + correction learning.**
   - `/analyze` stores its raw result in `scan_results` (30-day TTL).
   - `/logs` attaches it as `ai_original` per item (scaled to the saved weight), which revives
     `_capture_corrections`, and saves `ai_kcal` on the log.
   - % of scans edited = logs where `|totals.calories_kcal − ai_kcal| / ai_kcal > 5%`.

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

**Not done yet, on purpose:** the Play Store app still calls these endpoints. Remove them in the same
release as the v3 app, not before. (Item 11 is being done on `feat/free-and-premium-only`.)

Migration: data-only fields can stay in Mongo, but stop reading them. Don't drop collections without
a backup check (nightly backup at 03:30 UTC).

## Add (to serve the redesign)

13. ✅ **One logging streak.** Already there: `GET /api/streaks` → `food_logging: {current, best}`.
14. **Slim check-in.** `/api/daily-checkin` → weight + sleep (+ optional water) only, plus supplements
    when supplement tracking is on. It currently also carries gym and IF.
15. ✅ **Demo scan before signup.** `POST /api/food/analyze-demo`, no auth:
    - 3 per IP per day, 500 per day in total (in memory, resets on restart).
    - No MinIO upload, no log write.
    - Same response shape as `/analyze`.
16. ✅ **Today in one call.** Already there: `GET /api/summary?date=` returns food totals, goals,
    sleep, weight and supplements. Meals by slot: `GET /api/food/daily-totals-by-slot`.
17. **Before/after share card.** Client-side: render the compare widget to an image (Flutter
    `RepaintBoundary.toImage`) and share it. No server image rendering, and no body photo behind a URL.
18. ✅ **Weekly insight, food-only.** Already the case: `services/weekly_insights.py` prompt: intake, protein, logging
    consistency and weight trend vs logged kcal. No burn.
19. ✅ **Localised food names.** `lang` (hi/te/ta/kn/ml/mr/bn/gu) on `/analyze`, `/analyze-demo` and
    `/describe` adds `name_local` per item; `name` stays English.

## Already done (2026-10-30)

- Single-call scan (items carry grams + macros): 163 → ~105 kcal MAE on Nutrition5k.
- Scan returns `added_fat_g` and `pieces` per item (#42).
- Barcode lookup with nutrition-label fallback (#40).
- Plain-text OTP email; body photos filter by `days`.
