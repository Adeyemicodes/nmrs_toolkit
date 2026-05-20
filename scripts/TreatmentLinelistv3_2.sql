
/*
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 * ╔═══════════════════════════════════════════════════════════════════════════════════════════════════════════╗
 * ║                         TreatmentLinelistv3 - ARTLinelist Report (OPTIMIZED)                              ║
 * ╚═══════════════════════════════════════════════════════════════════════════════════════════════════════════╝
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 *
 * Description:  Optimized ARTLinelist report addressing MySQL temporary table limitations.
 *               Master temp tables are built in batch (efficient), then split into
 *               individual single-use tables (one per form+concept pair) before the main SELECT.
 *               Includes patient name resolution with _last_pickup_162240 fallback, patient address,
 *		  and removes the pretended +234 to retain original value of 0 on the phone number cols.
 *
 * Version:      v3.2
 * Source:       ARTLinelist_Report_FROM_nmrsreports-1.0.6.5_290126
 * Updated:      April 21, 2026
 * Modifier:     Adeyemi
 *
 * ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 * │                               🏥 Property of Caritas Nigeria                                            │
 * │                          Developed for Healthcare Data Excellence                                       │
 * └─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
 *
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 */

SET SESSION optimizer_switch = 'block_nested_loop=off';
SET GLOBAL innodb_buffer_pool_size = 8589934592;

SET @endDate = NOW();

-- Previous-quarter end date (replaces getendofquarter function)
SET @_pq = DATE_SUB(@endDate, INTERVAL 3 MONTH);
SET @prevQ_endDate = LAST_DAY(
    DATE_ADD(DATE_FORMAT(@_pq, '%Y-%m-01'),
             INTERVAL (QUARTER(@_pq) * 3 - 1) MONTH)
);

-- ===========================================================================
-- MASTER TABLE A: latest obs by (person, form, concept) — current period
-- ===========================================================================

DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form;

CREATE TEMPORARY TABLE _mst_latest_by_form (
    person_id     INT      NOT NULL,
    form_id       SMALLINT NOT NULL,
    concept_id    INT      NOT NULL,
    obs_id        INT      NOT NULL,
    encounter_id  INT,
    obs_datetime  DATETIME,
    value_numeric DOUBLE,
    value_coded   INT,
    value_datetime DATETIME,
    value_text    TEXT,
    obs_group_id  INT,
    KEY idx_pfc (person_id, form_id, concept_id)
) ENGINE=InnoDB;

-- Step 1: max datetime per (person, form, concept)
DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form_k;
CREATE TEMPORARY TABLE _mst_latest_by_form_k (
    person_id  INT NOT NULL, form_id SMALLINT NOT NULL,
    concept_id INT NOT NULL, max_dt  DATETIME NOT NULL,
    KEY k (person_id, form_id, concept_id)
) ENGINE=MEMORY
SELECT o.person_id, e.form_id, o.concept_id, MAX(o.obs_datetime) AS max_dt
FROM obs o
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.voided = 0
WHERE o.voided = 0
  AND o.obs_datetime <= @endDate
  AND e.form_id   IN (13,14,16,21,27,53,56,73)
  AND o.concept_id IN (165567,162240,165708,5096,165050,5089,1659,167139,
                       167150,856,5497,159951,165470,164852,166096,165994,
                       166007,166008)
GROUP BY o.person_id, e.form_id, o.concept_id;

-- Step 2: tiebreak by max obs_id
DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form_id;
CREATE TEMPORARY TABLE _mst_latest_by_form_id (
    person_id INT NOT NULL, form_id SMALLINT NOT NULL,
    concept_id INT NOT NULL, obs_id  INT NOT NULL,
    KEY k (obs_id)
) ENGINE=MEMORY
SELECT k.person_id, k.form_id, k.concept_id, MAX(o.obs_id) AS obs_id
FROM _mst_latest_by_form_k k
INNER JOIN obs o
    ON  o.person_id    = k.person_id AND o.concept_id  = k.concept_id
    AND o.obs_datetime = k.max_dt    AND o.voided = 0
INNER JOIN encounter e
    ON  e.encounter_id = o.encounter_id AND e.form_id = k.form_id AND e.voided = 0
GROUP BY k.person_id, k.form_id, k.concept_id;

-- Step 3: pull full obs row
INSERT INTO _mst_latest_by_form
SELECT i.person_id, i.form_id, o.concept_id, o.obs_id, o.encounter_id,
       o.obs_datetime, o.value_numeric, o.value_coded, o.value_datetime,
       o.value_text, o.obs_group_id
FROM _mst_latest_by_form_id i
INNER JOIN obs o ON o.obs_id = i.obs_id;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form_k;
DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form_id;


-- ===========================================================================
-- MASTER TABLE B: latest obs by (person, form, concept) — previous quarter
-- ===========================================================================

DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter;

CREATE TEMPORARY TABLE _mst_prev_quarter (
    person_id    INT NOT NULL, form_id    SMALLINT NOT NULL,
    concept_id   INT NOT NULL, obs_id     INT NOT NULL,
    encounter_id INT, obs_datetime DATETIME,
    value_coded  INT, obs_group_id INT,
    KEY idx_pfc (person_id, form_id, concept_id)
) ENGINE=InnoDB;

DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter_k;
CREATE TEMPORARY TABLE _mst_prev_quarter_k (
    person_id INT NOT NULL, form_id SMALLINT NOT NULL,
    concept_id INT NOT NULL, max_dt DATETIME NOT NULL,
    KEY k (person_id, form_id, concept_id)
) ENGINE=MEMORY
SELECT o.person_id, e.form_id, o.concept_id, MAX(o.obs_datetime) AS max_dt
FROM obs o
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.voided = 0
WHERE o.voided = 0
  AND o.obs_datetime <= @prevQ_endDate
  AND e.form_id   IN (13, 27)
  AND o.concept_id IN (162240, 165470)
GROUP BY o.person_id, e.form_id, o.concept_id;

DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter_id;
CREATE TEMPORARY TABLE _mst_prev_quarter_id (
    person_id INT NOT NULL, form_id SMALLINT NOT NULL,
    concept_id INT NOT NULL, obs_id INT NOT NULL,
    KEY k (obs_id)
) ENGINE=MEMORY
SELECT k.person_id, k.form_id, k.concept_id, MAX(o.obs_id) AS obs_id
FROM _mst_prev_quarter_k k
INNER JOIN obs o
    ON  o.person_id = k.person_id AND o.concept_id = k.concept_id
    AND o.obs_datetime = k.max_dt AND o.voided = 0
INNER JOIN encounter e
    ON  e.encounter_id = o.encounter_id AND e.form_id = k.form_id AND e.voided = 0
GROUP BY k.person_id, k.form_id, k.concept_id;

INSERT INTO _mst_prev_quarter
SELECT i.person_id, i.form_id, o.concept_id, o.obs_id, o.encounter_id,
       o.obs_datetime, o.value_coded, o.obs_group_id
FROM _mst_prev_quarter_id i
INNER JOIN obs o ON o.obs_id = i.obs_id;

DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter_k;
DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter_id;


-- ===========================================================================
-- MASTER TABLE C: latest obs, any form (form != 48)
-- ===========================================================================

DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form;

CREATE TEMPORARY TABLE _mst_latest_any_form (
    person_id    INT NOT NULL, concept_id   INT NOT NULL,
    obs_id       INT NOT NULL, encounter_id INT,
    obs_datetime DATETIME,     value_coded  INT,
    value_datetime DATETIME,   value_text   TEXT,
    KEY idx_pc (person_id, concept_id)
) ENGINE=InnoDB;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form_k;
CREATE TEMPORARY TABLE _mst_latest_any_form_k (
    person_id INT NOT NULL, concept_id INT NOT NULL, max_dt DATETIME NOT NULL,
    KEY k (person_id, concept_id)
) ENGINE=MEMORY
SELECT o.person_id, o.concept_id, MAX(o.obs_datetime) AS max_dt
FROM obs o
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.form_id != 48 AND e.voided = 0
WHERE o.voided = 0 AND o.obs_datetime <= @endDate
  AND o.concept_id IN (159599,160540,160554,160534,165242,165775,
                       165469,159635,160642,1113,159431)
GROUP BY o.person_id, o.concept_id;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form_id;
CREATE TEMPORARY TABLE _mst_latest_any_form_id (
    person_id INT NOT NULL, concept_id INT NOT NULL, obs_id INT NOT NULL,
    KEY k (obs_id)
) ENGINE=MEMORY
SELECT k.person_id, k.concept_id, MAX(o.obs_id) AS obs_id
FROM _mst_latest_any_form_k k
INNER JOIN obs o
    ON o.person_id = k.person_id AND o.concept_id = k.concept_id
    AND o.obs_datetime = k.max_dt AND o.voided = 0
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.form_id != 48 AND e.voided = 0
GROUP BY k.person_id, k.concept_id;

INSERT INTO _mst_latest_any_form
SELECT i.person_id, o.concept_id, o.obs_id, o.encounter_id,
       o.obs_datetime, o.value_coded, o.value_datetime, o.value_text
FROM _mst_latest_any_form_id i
INNER JOIN obs o ON o.obs_id = i.obs_id;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form_k;
DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form_id;


-- ===========================================================================
-- MASTER TABLE D: EARLIEST obs, any form — initial regimen concepts
-- ===========================================================================

DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form;

CREATE TEMPORARY TABLE _mst_first_any_form (
    person_id    INT NOT NULL, concept_id   INT NOT NULL,
    obs_id       INT NOT NULL, encounter_id INT,
    obs_datetime DATETIME,     value_coded  INT,
    KEY idx_pc (person_id, concept_id)
) ENGINE=InnoDB;

DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form_k;
CREATE TEMPORARY TABLE _mst_first_any_form_k (
    person_id INT NOT NULL, concept_id INT NOT NULL, min_dt DATETIME NOT NULL,
    KEY k (person_id, concept_id)
) ENGINE=MEMORY
SELECT o.person_id, o.concept_id, MIN(o.obs_datetime) AS min_dt
FROM obs o
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.form_id != 48 AND e.voided = 0
WHERE o.voided = 0 AND o.obs_datetime <= @endDate
  AND o.concept_id IN (165708, 164506, 164507, 164513, 164514)
GROUP BY o.person_id, o.concept_id;

DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form_id;
CREATE TEMPORARY TABLE _mst_first_any_form_id (
    person_id INT NOT NULL, concept_id INT NOT NULL, obs_id INT NOT NULL,
    KEY k (obs_id)
) ENGINE=MEMORY
SELECT k.person_id, k.concept_id, MIN(o.obs_id) AS obs_id
FROM _mst_first_any_form_k k
INNER JOIN obs o
    ON o.person_id = k.person_id AND o.concept_id = k.concept_id
    AND o.obs_datetime = k.min_dt AND o.voided = 0
INNER JOIN encounter e ON e.encounter_id = o.encounter_id AND e.form_id != 48 AND e.voided = 0
GROUP BY k.person_id, k.concept_id;

INSERT INTO _mst_first_any_form
SELECT i.person_id, o.concept_id, o.obs_id, o.encounter_id, o.obs_datetime, o.value_coded
FROM _mst_first_any_form_id i
INNER JOIN obs o ON o.obs_id = i.obs_id;

DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form_k;
DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form_id;


-- ===========================================================================
-- MASTER TABLE E: last encounter per (patient, form)
-- ===========================================================================

DROP TEMPORARY TABLE IF EXISTS _mst_last_encounter;
CREATE TEMPORARY TABLE _mst_last_encounter (
    patient_id INT NOT NULL, form_id SMALLINT NOT NULL,
    encounter_id INT NOT NULL, encounter_datetime DATETIME,
    KEY idx_pf (patient_id, form_id)
) ENGINE=InnoDB
SELECT e.patient_id, e.form_id, MAX(e.encounter_id) AS encounter_id,
       MAX(e.encounter_datetime) AS encounter_datetime
FROM encounter e
WHERE e.voided = 0 AND e.encounter_datetime <= @endDate
  AND e.form_id IN (27, 67, 69, 73)
GROUP BY e.patient_id, e.form_id;


-- ===========================================================================
-- SPLIT: derive one table per (form, concept) from each master
-- This is required because MySQL 5.7 cannot open the same temp table
-- more than once within a single SELECT statement.
-- ===========================================================================

-- ---- from _mst_latest_by_form ----
DROP TEMPORARY TABLE IF EXISTS _f16_anc_num_165567;  CREATE TEMPORARY TABLE _f16_anc_num_165567  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=16  AND concept_id=165567;
-- Unified pickup fallback (concept 162240): prefer form 27, fall back to form 13.
-- Built directly from _mst_latest_by_form using the 3-step key pattern to avoid Error 1137
-- (MySQL cannot open the same temp table more than once per query).
-- Step 1: latest obs_datetime per person across forms 13 and 27
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k1;
CREATE TEMPORARY TABLE _pickup_fallback_k1 (
    person_id INT NOT NULL, max_dt DATETIME NOT NULL, KEY k(person_id)
) ENGINE=MEMORY
SELECT person_id, MAX(obs_datetime) AS max_dt
FROM _mst_latest_by_form WHERE concept_id=162240 AND form_id IN (13,27)
GROUP BY person_id;
-- Step 2: preferred form at that datetime (MAX form_id = 27 beats 13 on tie)
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k2;
CREATE TEMPORARY TABLE _pickup_fallback_k2 (
    person_id INT NOT NULL, max_dt DATETIME NOT NULL, best_form SMALLINT NOT NULL, KEY k(person_id)
) ENGINE=MEMORY
SELECT k.person_id, k.max_dt, MAX(m.form_id) AS best_form
FROM _pickup_fallback_k1 k
INNER JOIN _mst_latest_by_form m ON m.person_id=k.person_id AND m.obs_datetime=k.max_dt
    AND m.concept_id=162240 AND m.form_id IN (13,27)
GROUP BY k.person_id, k.max_dt;
-- Step 3: tiebreak by max obs_id within chosen form
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k3;
CREATE TEMPORARY TABLE _pickup_fallback_k3 (
    person_id INT NOT NULL, obs_id INT NOT NULL, KEY k(obs_id)
) ENGINE=MEMORY
SELECT k.person_id, MAX(m.obs_id) AS obs_id
FROM _pickup_fallback_k2 k
INNER JOIN _mst_latest_by_form m ON m.person_id=k.person_id AND m.form_id=k.best_form
    AND m.obs_datetime=k.max_dt AND m.concept_id=162240
GROUP BY k.person_id;
-- Step 4: pull full row
DROP TEMPORARY TABLE IF EXISTS _last_pickup_162240;
CREATE TEMPORARY TABLE _last_pickup_162240 (PRIMARY KEY(person_id)) ENGINE=InnoDB
SELECT k.person_id, m.obs_id, m.encounter_id, m.obs_datetime,
       m.value_numeric, m.value_coded, m.value_datetime, m.value_text, m.obs_group_id
FROM _pickup_fallback_k3 k
INNER JOIN _mst_latest_by_form m ON m.obs_id=k.obs_id;
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k1;
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k2;
DROP TEMPORARY TABLE IF EXISTS _pickup_fallback_k3;
DROP TEMPORARY TABLE IF EXISTS _f27_regimen_line_165708;  CREATE TEMPORARY TABLE _f27_regimen_line_165708  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=27  AND concept_id=165708;
DROP TEMPORARY TABLE IF EXISTS _f27_next_appt_5096;    CREATE TEMPORARY TABLE _f27_next_appt_5096    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=27  AND concept_id=5096;
DROP TEMPORARY TABLE IF EXISTS _f14_pregnancy_165050;  CREATE TEMPORARY TABLE _f14_pregnancy_165050  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=165050;
DROP TEMPORARY TABLE IF EXISTS _f14_next_appt_5096;    CREATE TEMPORARY TABLE _f14_next_appt_5096    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=5096;
DROP TEMPORARY TABLE IF EXISTS _f14_weight_5089;    CREATE TEMPORARY TABLE _f14_weight_5089    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=5089;
DROP TEMPORARY TABLE IF EXISTS _f14_tb_status_1659;    CREATE TEMPORARY TABLE _f14_tb_status_1659    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=1659;
DROP TEMPORARY TABLE IF EXISTS _f14_cx_cancer_screen_167139;  CREATE TEMPORARY TABLE _f14_cx_cancer_screen_167139  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=167139;
DROP TEMPORARY TABLE IF EXISTS _f14_cx_cancer_treat_167150;  CREATE TEMPORARY TABLE _f14_cx_cancer_treat_167150  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=14  AND concept_id=167150;
DROP TEMPORARY TABLE IF EXISTS _f21_viral_load_856;     CREATE TEMPORARY TABLE _f21_viral_load_856     (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=21  AND concept_id=856;
DROP TEMPORARY TABLE IF EXISTS _f21_cd4_count_5497;    CREATE TEMPORARY TABLE _f21_cd4_count_5497    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=21  AND concept_id=5497;
DROP TEMPORARY TABLE IF EXISTS _f21_sample_date_159951;  CREATE TEMPORARY TABLE _f21_sample_date_159951  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=21  AND concept_id=159951;
DROP TEMPORARY TABLE IF EXISTS _f13_outcome_165470;  CREATE TEMPORARY TABLE _f13_outcome_165470  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=13  AND concept_id=165470;
DROP TEMPORARY TABLE IF EXISTS _f56_inh_start_164852;  CREATE TEMPORARY TABLE _f56_inh_start_164852  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=56  AND concept_id=164852;
DROP TEMPORARY TABLE IF EXISTS _f56_inh_stop_166096;  CREATE TEMPORARY TABLE _f56_inh_stop_166096  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=56  AND concept_id=166096;
DROP TEMPORARY TABLE IF EXISTS _f53_inh_start_165994;  CREATE TEMPORARY TABLE _f53_inh_start_165994  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=53  AND concept_id=165994;
DROP TEMPORARY TABLE IF EXISTS _f53_inh_outcome_166007;  CREATE TEMPORARY TABLE _f53_inh_outcome_166007  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=53  AND concept_id=166007;
DROP TEMPORARY TABLE IF EXISTS _f53_inh_outcome_date_166008;  CREATE TEMPORARY TABLE _f53_inh_outcome_date_166008  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=53  AND concept_id=166008;
DROP TEMPORARY TABLE IF EXISTS _f73_otz_outcome_date_166008;  CREATE TEMPORARY TABLE _f73_otz_outcome_date_166008  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_numeric,value_coded,value_datetime,value_text,obs_group_id FROM _mst_latest_by_form WHERE form_id=73  AND concept_id=166008;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_by_form;

-- ---- from _mst_prev_quarter ----
DROP TEMPORARY TABLE IF EXISTS _pq_pickup_date_162240; CREATE TEMPORARY TABLE _pq_pickup_date_162240 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,obs_group_id FROM _mst_prev_quarter WHERE form_id=27 AND concept_id=162240;
DROP TEMPORARY TABLE IF EXISTS _pq_outcome_165470; CREATE TEMPORARY TABLE _pq_outcome_165470 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,obs_datetime,value_coded FROM _mst_prev_quarter WHERE form_id=13 AND concept_id=165470;

DROP TEMPORARY TABLE IF EXISTS _mst_prev_quarter;

-- ---- from _mst_latest_any_form ----
DROP TEMPORARY TABLE IF EXISTS _a_art_start_159599;  CREATE TEMPORARY TABLE _a_art_start_159599  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=159599;
DROP TEMPORARY TABLE IF EXISTS _a_care_entry_160540;  CREATE TEMPORARY TABLE _a_care_entry_160540  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=160540;
DROP TEMPORARY TABLE IF EXISTS _a_hiv_confirmed_160554;  CREATE TEMPORARY TABLE _a_hiv_confirmed_160554  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=160554;
DROP TEMPORARY TABLE IF EXISTS _a_transfer_in_date_160534;  CREATE TEMPORARY TABLE _a_transfer_in_date_160534  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=160534;
DROP TEMPORARY TABLE IF EXISTS _a_transfer_in_status_165242;  CREATE TEMPORARY TABLE _a_transfer_in_status_165242  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=165242;
DROP TEMPORARY TABLE IF EXISTS _a_returned_to_care_165775;  CREATE TEMPORARY TABLE _a_returned_to_care_165775  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=165775;
DROP TEMPORARY TABLE IF EXISTS _a_termination_date_165469;  CREATE TEMPORARY TABLE _a_termination_date_165469  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=165469;
DROP TEMPORARY TABLE IF EXISTS _a_nok_phone_159635;  CREATE TEMPORARY TABLE _a_nok_phone_159635  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=159635;
DROP TEMPORARY TABLE IF EXISTS _a_ts_phone_160642;  CREATE TEMPORARY TABLE _a_ts_phone_160642  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=160642;
DROP TEMPORARY TABLE IF EXISTS _a_baseline_tb_start_1113;    CREATE TEMPORARY TABLE _a_baseline_tb_start_1113    (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=1113;
DROP TEMPORARY TABLE IF EXISTS _a_baseline_tb_stop_159431;  CREATE TEMPORARY TABLE _a_baseline_tb_stop_159431  (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded,value_datetime,value_text FROM _mst_latest_any_form WHERE concept_id=159431;

DROP TEMPORARY TABLE IF EXISTS _mst_latest_any_form;

-- ---- from _mst_first_any_form ----
DROP TEMPORARY TABLE IF EXISTS _fo_initial_regimen_line_165708; CREATE TEMPORARY TABLE _fo_initial_regimen_line_165708 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded FROM _mst_first_any_form WHERE concept_id=165708;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_1st_line_164506; CREATE TEMPORARY TABLE _fo_initial_1st_line_164506 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded FROM _mst_first_any_form WHERE concept_id=164506;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_1st_line_alt_164507; CREATE TEMPORARY TABLE _fo_initial_1st_line_alt_164507 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded FROM _mst_first_any_form WHERE concept_id=164507;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_2nd_line_164513; CREATE TEMPORARY TABLE _fo_initial_2nd_line_164513 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded FROM _mst_first_any_form WHERE concept_id=164513;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_2nd_line_alt_164514; CREATE TEMPORARY TABLE _fo_initial_2nd_line_alt_164514 (PRIMARY KEY(person_id)) ENGINE=InnoDB SELECT person_id,obs_id,encounter_id,obs_datetime,value_coded FROM _mst_first_any_form WHERE concept_id=164514;

DROP TEMPORARY TABLE IF EXISTS _mst_first_any_form;

-- ---- from _mst_last_encounter ----
DROP TEMPORARY TABLE IF EXISTS _enc_pharmacy_27; CREATE TEMPORARY TABLE _enc_pharmacy_27 (PRIMARY KEY(patient_id)) ENGINE=InnoDB SELECT patient_id,encounter_id,encounter_datetime FROM _mst_last_encounter WHERE form_id=27;
DROP TEMPORARY TABLE IF EXISTS _enc_vl_sample_67; CREATE TEMPORARY TABLE _enc_vl_sample_67 (PRIMARY KEY(patient_id)) ENGINE=InnoDB SELECT patient_id,encounter_id,encounter_datetime FROM _mst_last_encounter WHERE form_id=67;
DROP TEMPORARY TABLE IF EXISTS _enc_eac_69; CREATE TEMPORARY TABLE _enc_eac_69 (PRIMARY KEY(patient_id)) ENGINE=InnoDB SELECT patient_id,encounter_id,encounter_datetime FROM _mst_last_encounter WHERE form_id=69;
DROP TEMPORARY TABLE IF EXISTS _enc_otz_73; CREATE TEMPORARY TABLE _enc_otz_73 (PRIMARY KEY(patient_id)) ENGINE=InnoDB SELECT patient_id,encounter_id,encounter_datetime FROM _mst_last_encounter WHERE form_id=73;

DROP TEMPORARY TABLE IF EXISTS _mst_last_encounter;


-- ===========================================================================
-- STANDALONE TEMP TABLES (single-use, no split needed)
-- ===========================================================================

-- Earliest CD4 (form 21, concept 5497)
DROP TEMPORARY TABLE IF EXISTS _min_cd4;
CREATE TEMPORARY TABLE _min_cd4 (
    person_id INT NOT NULL, obs_id INT NOT NULL,
    obs_datetime DATETIME, value_numeric DOUBLE,
    PRIMARY KEY (person_id)
) ENGINE=InnoDB;

DROP TEMPORARY TABLE IF EXISTS _min_cd4_key;
CREATE TEMPORARY TABLE _min_cd4_key (person_id INT NOT NULL, min_dt DATETIME NOT NULL, KEY k(person_id)) ENGINE=MEMORY
SELECT o.person_id, MIN(o.obs_datetime) AS min_dt
FROM obs o
INNER JOIN encounter e ON e.encounter_id=o.encounter_id AND e.form_id=21 AND e.voided=0
WHERE o.concept_id=5497 AND o.voided=0 AND o.obs_datetime<=@endDate
GROUP BY o.person_id;

INSERT INTO _min_cd4
SELECT k.person_id, MIN(o.obs_id), k.min_dt,
       (SELECT o2.value_numeric FROM obs o2 WHERE o2.obs_id=MIN(o.obs_id))
FROM _min_cd4_key k
INNER JOIN obs o
    ON o.person_id=k.person_id AND o.concept_id=5497
    AND o.obs_datetime=k.min_dt AND o.voided=0
INNER JOIN encounter e ON e.encounter_id=o.encounter_id AND e.form_id=21 AND e.voided=0
GROUP BY k.person_id;

DROP TEMPORARY TABLE IF EXISTS _min_cd4_key;


-- Last visit across clinical forms
DROP TEMPORARY TABLE IF EXISTS _last_visit;
CREATE TEMPORARY TABLE _last_visit (
    patient_id INT NOT NULL, encounter_id INT NOT NULL,
    encounter_datetime DATETIME, PRIMARY KEY(patient_id)
) ENGINE=InnoDB
SELECT e.patient_id, MAX(e.encounter_id) AS encounter_id,
       MAX(e.encounter_datetime) AS encounter_datetime
FROM encounter e
WHERE e.voided=0 AND e.encounter_datetime<=@endDate
  AND e.form_id IN (22,56,14,69,23,44,74,53,21,73,20,27,67)
GROUP BY e.patient_id;


-- Latest INH dispensed (form 27, concept 165727, value_coded=1679)
DROP TEMPORARY TABLE IF EXISTS _inh_last_dispensed;
CREATE TEMPORARY TABLE _inh_last_dispensed (
    person_id INT NOT NULL, obs_datetime DATETIME, PRIMARY KEY(person_id)
) ENGINE=InnoDB
SELECT o.person_id, MAX(o.obs_datetime) AS obs_datetime
FROM obs o
INNER JOIN encounter e ON e.encounter_id=o.encounter_id AND e.form_id=27 AND e.voided=0
WHERE o.concept_id=165727 AND o.value_coded=1679 AND o.voided=0 AND o.obs_datetime<=@endDate
GROUP BY o.person_id;


-- ===========================================================================
-- MAIN QUERY
-- ===========================================================================

SELECT
nigeria_datimcode_mapping.state_name                                    AS `State`,
nigeria_datimcode_mapping.lga_name                                      AS `LGA`,
gp_datim.property_value                                                      AS DatimCode,
gp_facility.property_value                                                      AS FacilityName,
pid1.identifier                                                         AS `PatientUniqueID`,
pid2.identifier                                                         AS `PatientHospitalNo`,
pid3.identifier                                                         AS `ANCNoIdentifier`,
_f16_anc_num_165567.value_text                                                  AS `ANCNoConceptID`,
pid4.identifier                                                         AS `HTSNo`,
pn.given_name                                                           AS `FirstName`,
pn.family_name                                                          AS `Surname`,
person.gender                                                           AS `Sex`,

IF(TIMESTAMPDIFF(YEAR,person.birthdate,_a_art_start_159599.value_datetime)>=5,
   TIMESTAMPDIFF(YEAR,person.birthdate,_a_art_start_159599.value_datetime),0)    AS `AgeAtStartOfARTYears`,
IF(TIMESTAMPDIFF(YEAR,person.birthdate,_a_art_start_159599.value_datetime)<5,
   TIMESTAMPDIFF(MONTH,person.birthdate,_a_art_start_159599.value_datetime),NULL) AS `AgeAtStartOfARTMonths`,

(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_a_care_entry_160540.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS CareEntryPoint,
DATE_FORMAT(_a_hiv_confirmed_160554.value_datetime,'%d-%b-%Y')                       AS `HIVConfirmedDate`,
TIMESTAMPDIFF(MONTH,_a_art_start_159599.value_datetime,_last_pickup_162240.obs_datetime) AS MonthsOnART,
DATE_FORMAT(_a_transfer_in_date_160534.value_datetime,'%d-%b-%Y')                       AS DateTransferredIn,
(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_a_transfer_in_status_165242.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS TransferInStatus,
DATE_FORMAT(_a_art_start_159599.value_datetime,'%d-%b-%Y')                       AS `ARTStartDate`,
DATE_FORMAT(_last_pickup_162240.obs_datetime,'%d-%b-%Y')                       AS `LastPickupDate`,
DATE_FORMAT(_last_visit.encounter_datetime,'%d-%b-%Y')                     AS LastVisitDate,

(SELECT value_numeric FROM obs WHERE obs_group_id=_last_pickup_162240.obs_id AND concept_id=159368 AND person_id=patient.patient_id AND voided=0 LIMIT 1)
                                                                        AS `DaysOfARVRefil`,
COALESCE(CAST((SELECT value_text FROM obs WHERE encounter_id=_enc_pharmacy_27.encounter_id AND concept_id=166406 AND voided=0 LIMIT 1) AS SIGNED),'')
                                                                        AS `PillBalance`,

(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_fo_initial_regimen_line_165708.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `InitialRegimenLine`,
(SELECT cn.name FROM obs oir INNER JOIN concept_name cn ON cn.concept_id=oir.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE oir.concept_id=_fo_initial_regimen_line_165708.value_coded AND oir.encounter_id=_fo_initial_regimen_line_165708.encounter_id AND oir.voided=0 LIMIT 1)
                                                                        AS `InitialRegimen`,

_min_cd4.value_numeric                                                  AS `InitialCD4Count`,
DATE_FORMAT(_min_cd4.obs_datetime,'%d-%b-%Y')                          AS `InitialCD4CountDate`,
_f21_cd4_count_5497.value_numeric                                                 AS `CurrentCD4Count`,
DATE_FORMAT(_f21_cd4_count_5497.obs_datetime,'%d-%b-%Y')                         AS `CurrentCD4CountDate`,
DATE_FORMAT(_enc_eac_69.encounter_datetime,'%d-%b-%Y')                        AS `LastEACDate`,

(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f27_regimen_line_165708.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `CurrentRegimenLine`,
(SELECT cn.name FROM obs ocr INNER JOIN concept_name cn ON cn.concept_id=ocr.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE ocr.concept_id=_f27_regimen_line_165708.value_coded AND ocr.encounter_id=_f27_regimen_line_165708.encounter_id AND ocr.voided=0 LIMIT 1)
                                                                        AS `CurrentRegimen`,

IF(person.gender='F',(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f14_pregnancy_165050.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),NULL)
                                                                        AS `PregnancyStatus`,
IF(person.gender='F',DATE_FORMAT(_f14_pregnancy_165050.obs_datetime,'%d-%b-%Y'),NULL)
                                                                        AS `PregnancyStatusDate`,
IF(person.gender='F',DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f14_pregnancy_165050.encounter_id AND concept_id=5596 AND voided=0 LIMIT 1),'%d-%b-%Y'),NULL)
                                                                        AS `EDD`,
IF(person.gender='F',DATE_FORMAT(_f14_pregnancy_165050.value_datetime,'%d-%b-%Y'),NULL)
                                                                        AS `LastDeliveryDate`,
IF(person.gender='F',DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f14_pregnancy_165050.encounter_id AND concept_id=1427 AND voided=0 LIMIT 1),'%d-%b-%Y'),NULL)
                                                                        AS `LMP`,
IF(person.gender='F',(SELECT value_numeric FROM obs WHERE encounter_id=_f14_pregnancy_165050.encounter_id AND concept_id=1438 AND voided=0 LIMIT 1),NULL)
                                                                        AS `GestationAgeWeeks`,

_f21_viral_load_856.value_numeric                                                  AS `CurrentViralLoad(c/ml)`,
DATE_FORMAT(_f21_viral_load_856.obs_datetime,'%d-%b-%Y')                          AS `ViralLoadEncounterDate`,
DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f21_viral_load_856.encounter_id AND concept_id=159951 AND voided=0 LIMIT 1),'%d-%b-%Y')
                                                                        AS `ViralLoadSampleCollectionDate`,
DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f21_viral_load_856.encounter_id AND concept_id=165414 AND person_id=patient.patient_id AND voided=0 LIMIT 1),'%d-%b-%Y')
                                                                        AS `ViralLoadReportedDate`,
DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f21_viral_load_856.encounter_id AND concept_id=166423 AND voided=0 LIMIT 1),'%d-%b-%Y')
                                                                        AS `ResultDate`,
DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f21_viral_load_856.encounter_id AND concept_id=166424 AND voided=0 LIMIT 1),'%d-%b-%Y')
                                                                        AS `AssayDate`,
DATE_FORMAT((SELECT value_datetime FROM obs WHERE encounter_id=_f21_viral_load_856.encounter_id AND concept_id=166425 AND voided=0 LIMIT 1),'%d-%b-%Y')
                                                                        AS `ApprovalDate`,
(SELECT cn.name FROM obs ovi INNER JOIN concept_name cn ON cn.concept_id=ovi.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE ovi.encounter_id=_f21_viral_load_856.encounter_id AND ovi.concept_id=164980 AND ovi.voided=0 LIMIT 1)
                                                                        AS `ViralLoadIndication`,

(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f13_outcome_165470.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS PatientOutcome,
DATE_FORMAT(_f13_outcome_165470.obs_datetime,'%d-%b-%Y')                       AS PatientOutcomeDate,

IFNULL(
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f13_outcome_165470.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),
  IF(_last_pickup_162240.obs_datetime IS NULL, NULL,
     IF(DATEDIFF(DATE_ADD(_last_pickup_162240.obs_datetime, INTERVAL (COALESCE((SELECT value_numeric FROM obs WHERE obs_group_id=_last_pickup_162240.obs_id AND concept_id=159368 AND person_id=patient.patient_id AND voided=0 LIMIT 1),0)+28) DAY),
        IF(@endDate IS NULL OR @endDate='',CURDATE(),@endDate))>=0,'Active','LTFU'))
)                                                                       AS `CurrentARTStatus`,

(SELECT cn.name FROM obs odm INNER JOIN concept_name cn ON cn.concept_id=odm.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE odm.encounter_id=_last_pickup_162240.encounter_id AND odm.concept_id=166148 AND odm.voided=0 LIMIT 1)
                                                                        AS `DispensingModality`,
(SELECT cn.name FROM obs ofdm INNER JOIN concept_name cn ON cn.concept_id=ofdm.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE ofdm.encounter_id=_last_pickup_162240.encounter_id AND ofdm.concept_id=166276 AND ofdm.voided=0 LIMIT 1)
                                                                        AS `FacilityDispensingModality`,
(SELECT cn.name FROM obs oddd INNER JOIN concept_name cn ON cn.concept_id=oddd.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE oddd.encounter_id=_last_pickup_162240.encounter_id AND oddd.concept_id=166363 AND oddd.voided=0 LIMIT 1)
                                                                        AS `DDDDispensingModality`,
(SELECT cn.name FROM obs ommd INNER JOIN concept_name cn ON cn.concept_id=ommd.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE ommd.encounter_id=_last_pickup_162240.encounter_id AND ommd.concept_id=166278 AND ommd.voided=0 LIMIT 1)
                                                                        AS `MMDType`,

DATE_FORMAT(_a_returned_to_care_165775.value_datetime,'%d-%b-%Y')                       AS `DateReturnedToCare`,
DATE_FORMAT(_a_termination_date_165469.value_datetime,'%d-%b-%Y')                       AS `DateOfTermination`,
DATE_FORMAT(_f27_next_appt_5096.value_datetime,'%d-%b-%Y')                       AS `PharmacyNextAppointment`,
DATE_FORMAT(_f14_next_appt_5096.value_datetime,'%d-%b-%Y')                       AS `ClinicalNextAppointment`,

IF(TIMESTAMPDIFF(YEAR,person.birthdate,CURDATE())>=5, TIMESTAMPDIFF(YEAR,person.birthdate,CURDATE()),NULL)  AS `CurrentAgeYears`,
IF(TIMESTAMPDIFF(YEAR,person.birthdate,CURDATE())<5,  TIMESTAMPDIFF(MONTH,person.birthdate,CURDATE()),NULL) AS `CurrentAgeMonths`,
DATE_FORMAT(person.birthdate,'%d-%b-%Y')                               AS `DateOfBirth`,
IF(person.dead=1,'Dead','')                                            AS MarkAsDeseased,
IF(person.dead=1,person.death_date,'')                                 AS MarkAsDeseasedDeathDate,

phone_attr.value                                                         AS RegistrationPhoneNo,
_a_nok_phone_159635.value_text                                           AS `NextofKinPhoneNo`,
_a_ts_phone_160642.value_text                                            AS `TreatmentSupporterPhoneNo`,
addr.Address                                                           AS `Address`,

IF(biometrictable.patient_Id IS NOT NULL,'Yes','No')                   AS BiometricCaptured,
IF(biometrictable.patient_Id IS NOT NULL,DATE_FORMAT(biometrictable.date_created,'%d-%b-%Y'),'')
                                                                        AS BiometricCaptureDate,
IF(biometrictable.patient_Id IS NOT NULL,IF(invalidprint.patient_Id IS NOT NULL,'No','Yes'),'')
                                                                        AS ValidCapture,

_f14_weight_5089.value_numeric                                                 AS `CurrentWeight(Kg)`,
DATE_FORMAT(_f14_weight_5089.obs_datetime,'%d-%b-%Y')                         AS `CurrentWeightDate`,
(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f14_tb_status_1659.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `TBStatus`,
DATE_FORMAT(_f14_tb_status_1659.obs_datetime,'%d-%b-%Y')                         AS `TBStatusDate`,
DATE_FORMAT(_f56_inh_start_164852.value_datetime,'%d-%b-%Y')                     AS `BaselineINHStartDate`,
DATE_FORMAT(_f56_inh_stop_166096.value_datetime,'%d-%b-%Y')                     AS `BaselineINHStopDate`,
DATE_FORMAT(_f53_inh_start_165994.value_datetime,'%d-%b-%Y')                     AS `CurrentINHStartDate`,
(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f53_inh_outcome_166007.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `CurrentINHOutcome`,
DATE_FORMAT(_f53_inh_outcome_date_166008.value_datetime,'%d-%b-%Y')                     AS `CurrentINHOutcomeDate`,
DATE_FORMAT(_inh_last_dispensed.obs_datetime,'%d-%b-%Y')                         AS `LastINHDispensedDate`,
DATE_FORMAT(_a_baseline_tb_start_1113.value_datetime,'%d-%b-%Y')                         AS `BaselineTBTreatmentStartDate`,
DATE_FORMAT(_a_baseline_tb_stop_159431.value_datetime,'%d-%b-%Y')                       AS `BaselineTBTreatmentStopDate`,
DATE_FORMAT(_enc_vl_sample_67.encounter_datetime,'%d-%b-%Y')                        AS `LastViralLoadSampleCollectionFormDate`,
DATE_FORMAT(_f21_sample_date_159951.value_datetime,'%d-%b-%Y')                     AS `LastSampleTakenDate`,
DATE_FORMAT(_enc_otz_73.encounter_datetime,'%d-%b-%Y')                        AS `OTZEnrollmentDate`,
DATE_FORMAT(_f73_otz_outcome_date_166008.value_datetime,'%d-%b-%Y')                     AS `OTZOutcomeDate`,
DATE_FORMAT(patient_prog.date_enrolled,'%d-%b-%Y')                             AS EnrollmentDate,

COALESCE(
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_fo_initial_1st_line_164506.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_fo_initial_1st_line_alt_164507.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1))
                                                                        AS `InitialFirstLineRegimen`,
COALESCE(DATE_FORMAT(_fo_initial_1st_line_164506.obs_datetime,'%d-%b-%Y'),DATE_FORMAT(_fo_initial_1st_line_alt_164507.obs_datetime,'%d-%b-%Y'))
                                                                        AS `InitialFirstLineRegimenDate`,
COALESCE(
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_fo_initial_2nd_line_164513.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_fo_initial_2nd_line_alt_164514.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1))
                                                                        AS `InitialSecondLineRegimen`,
COALESCE(DATE_FORMAT(_fo_initial_2nd_line_164513.obs_datetime,'%d-%b-%Y'),DATE_FORMAT(_fo_initial_2nd_line_alt_164514.obs_datetime,'%d-%b-%Y'))
                                                                        AS `InitialSecondLineRegimenDate`,

DATE_FORMAT(_pq_pickup_date_162240.obs_datetime,'%d-%b-%Y')                      AS `LastPickupDatePreviousQuarter`,
(SELECT value_numeric FROM obs WHERE obs_group_id=_pq_pickup_date_162240.obs_id AND concept_id=159368 AND person_id=patient.patient_id AND voided=0 LIMIT 1)
                                                                        AS `DrugDurationPreviousQuarter`,
(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_pq_outcome_165470.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `PatientOutcomePreviousQuarter`,
DATE_FORMAT(_pq_outcome_165470.obs_datetime,'%d-%b-%Y')                      AS `PatientOutcomeDatePreviousQuarter`,

IFNULL(
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_pq_outcome_165470.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),
  IF(_pq_pickup_date_162240.obs_datetime IS NULL, NULL,
     IF(DATEDIFF(DATE_ADD(_pq_pickup_date_162240.obs_datetime, INTERVAL (COALESCE((SELECT value_numeric FROM obs WHERE obs_group_id=_pq_pickup_date_162240.obs_id AND concept_id=159368 AND person_id=patient.patient_id AND voided=0 LIMIT 1),0)+28) DAY),@prevQ_endDate)>=0,'Active','LTFU'))
)                                                                       AS `ARTStatusPreviousQuarter`,

(SELECT value_numeric FROM obs WHERE obs_group_id=_last_pickup_162240.obs_id AND concept_id=160856 AND person_id=patient.patient_id AND voided=0 LIMIT 1)
                                                                        AS `QuantityOfARVDispensedLastVisit`,
(SELECT cn.name FROM obs ofq INNER JOIN concept_name cn ON cn.concept_id=ofq.value_coded AND cn.locale='en' AND cn.locale_preferred=1 WHERE ofq.obs_group_id=_last_pickup_162240.obs_id AND ofq.concept_id=165723 AND ofq.person_id=patient.patient_id AND ofq.voided=0 LIMIT 1)
                                                                        AS `FrequencyOfARVDispensedLastVisit`,

IFNULL(
  (SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f13_outcome_165470.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1),
  IF(_last_pickup_162240.obs_datetime IS NULL,'',
     IF(DATEDIFF(DATE_ADD(_last_pickup_162240.obs_datetime, INTERVAL (COALESCE((SELECT value_numeric FROM obs WHERE obs_group_id=_last_pickup_162240.obs_id AND concept_id=159368 AND person_id=patient.patient_id AND voided=0 LIMIT 1),0)+28+COALESCE(CAST((SELECT value_text FROM obs WHERE encounter_id=_enc_pharmacy_27.encounter_id AND concept_id=166406 AND voided=0 LIMIT 1) AS SIGNED),0)) DAY),IF(@endDate IS NULL OR @endDate='',CURDATE(),@endDate))>=0,'Active','InActive'))
)                                                                       AS `CurrentARTStatusWithPillBalance`,

DATE_FORMAT(bvinfo.RecaptureDate,'%d-%b-%Y')                           AS RecaptureDate,
bvinfo.recapture_count                                                  AS RecaptureCount,

(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f14_cx_cancer_screen_167139.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `CervicalCancerScreeningStatus`,
DATE_FORMAT(_f14_cx_cancer_screen_167139.obs_datetime,'%d-%b-%Y')                       AS `CervicalCancerScreeningStatusDate`,
(SELECT cn.name FROM concept_name cn WHERE cn.concept_id=_f14_cx_cancer_treat_167150.value_coded AND cn.locale='en' AND cn.locale_preferred=1 LIMIT 1)
                                                                        AS `CervicalCancerTreatmentProvided`,
DATE_FORMAT(_f14_cx_cancer_treat_167150.obs_datetime,'%d-%b-%Y')                       AS `CervicalCancerTreatmentProvidedDate`


FROM patient
INNER JOIN person ON person.person_id = patient.patient_id AND patient.voided = 0
LEFT JOIN person_name pn ON pn.person_id = patient.patient_id AND pn.voided = 0 AND pn.preferred = 1

LEFT JOIN patient_identifier pid1 ON pid1.patient_id=patient.patient_id AND pid1.identifier_type=4 AND pid1.voided=0
LEFT JOIN patient_identifier pid2 ON pid2.patient_id=patient.patient_id AND pid2.identifier_type=5 AND pid2.voided=0
LEFT JOIN patient_identifier pid3 ON pid3.patient_id=patient.patient_id AND pid3.identifier_type=6 AND pid3.voided=0
LEFT JOIN patient_identifier pid4 ON pid4.patient_id=patient.patient_id AND pid4.identifier_type=8 AND pid4.voided=0
LEFT JOIN person_attribute  phone_attr ON phone_attr.person_id=person.person_id AND phone_attr.person_attribute_type_id=8 AND phone_attr.voided=0
LEFT JOIN patient_program   patient_prog    ON patient_prog.patient_id=patient.patient_id AND patient_prog.program_id=1 AND patient_prog.voided=0

-- latest obs by form+concept
LEFT JOIN _f16_anc_num_165567  ON _f16_anc_num_165567.person_id  = patient.patient_id
LEFT JOIN _last_pickup_162240   ON _last_pickup_162240.person_id   = patient.patient_id
LEFT JOIN _f27_regimen_line_165708  ON _f27_regimen_line_165708.person_id  = patient.patient_id
LEFT JOIN _f27_next_appt_5096    ON _f27_next_appt_5096.person_id    = patient.patient_id
LEFT JOIN _f14_pregnancy_165050  ON _f14_pregnancy_165050.person_id  = patient.patient_id
LEFT JOIN _f14_next_appt_5096    ON _f14_next_appt_5096.person_id    = patient.patient_id
LEFT JOIN _f14_weight_5089    ON _f14_weight_5089.person_id    = patient.patient_id
LEFT JOIN _f14_tb_status_1659    ON _f14_tb_status_1659.person_id    = patient.patient_id
LEFT JOIN _f14_cx_cancer_screen_167139  ON _f14_cx_cancer_screen_167139.person_id  = patient.patient_id
LEFT JOIN _f14_cx_cancer_treat_167150  ON _f14_cx_cancer_treat_167150.person_id  = patient.patient_id
LEFT JOIN _f21_viral_load_856     ON _f21_viral_load_856.person_id     = patient.patient_id
LEFT JOIN _f21_cd4_count_5497    ON _f21_cd4_count_5497.person_id    = patient.patient_id
LEFT JOIN _f21_sample_date_159951  ON _f21_sample_date_159951.person_id  = patient.patient_id
LEFT JOIN _f13_outcome_165470  ON _f13_outcome_165470.person_id  = patient.patient_id
LEFT JOIN _f56_inh_start_164852  ON _f56_inh_start_164852.person_id  = patient.patient_id
LEFT JOIN _f56_inh_stop_166096  ON _f56_inh_stop_166096.person_id  = patient.patient_id
LEFT JOIN _f53_inh_start_165994  ON _f53_inh_start_165994.person_id  = patient.patient_id
LEFT JOIN _f53_inh_outcome_166007  ON _f53_inh_outcome_166007.person_id  = patient.patient_id
LEFT JOIN _f53_inh_outcome_date_166008  ON _f53_inh_outcome_date_166008.person_id  = patient.patient_id
LEFT JOIN _f73_otz_outcome_date_166008  ON _f73_otz_outcome_date_166008.person_id  = patient.patient_id

-- previous quarter
LEFT JOIN _pq_pickup_date_162240 ON _pq_pickup_date_162240.person_id = patient.patient_id
LEFT JOIN _pq_outcome_165470 ON _pq_outcome_165470.person_id = patient.patient_id

-- any-form latest
LEFT JOIN _a_art_start_159599    ON _a_art_start_159599.person_id    = patient.patient_id
LEFT JOIN _a_care_entry_160540    ON _a_care_entry_160540.person_id    = patient.patient_id
LEFT JOIN _a_hiv_confirmed_160554    ON _a_hiv_confirmed_160554.person_id    = patient.patient_id
LEFT JOIN _a_transfer_in_date_160534    ON _a_transfer_in_date_160534.person_id    = patient.patient_id
LEFT JOIN _a_transfer_in_status_165242    ON _a_transfer_in_status_165242.person_id    = patient.patient_id
LEFT JOIN _a_returned_to_care_165775    ON _a_returned_to_care_165775.person_id    = patient.patient_id
LEFT JOIN _a_termination_date_165469    ON _a_termination_date_165469.person_id    = patient.patient_id
LEFT JOIN _a_nok_phone_159635    ON _a_nok_phone_159635.person_id    = patient.patient_id
LEFT JOIN _a_ts_phone_160642    ON _a_ts_phone_160642.person_id    = patient.patient_id
LEFT JOIN _a_baseline_tb_start_1113      ON _a_baseline_tb_start_1113.person_id      = patient.patient_id
LEFT JOIN _a_baseline_tb_stop_159431    ON _a_baseline_tb_stop_159431.person_id    = patient.patient_id

-- earliest obs (initial regimens)
LEFT JOIN _fo_initial_regimen_line_165708   ON _fo_initial_regimen_line_165708.person_id   = patient.patient_id
LEFT JOIN _fo_initial_1st_line_164506   ON _fo_initial_1st_line_164506.person_id   = patient.patient_id
LEFT JOIN _fo_initial_1st_line_alt_164507   ON _fo_initial_1st_line_alt_164507.person_id   = patient.patient_id
LEFT JOIN _fo_initial_2nd_line_164513   ON _fo_initial_2nd_line_164513.person_id   = patient.patient_id
LEFT JOIN _fo_initial_2nd_line_alt_164514   ON _fo_initial_2nd_line_alt_164514.person_id   = patient.patient_id

-- earliest CD4, last visit, last encounters, INH dispensed
LEFT JOIN _min_cd4     ON _min_cd4.person_id     = patient.patient_id
LEFT JOIN _last_visit      ON _last_visit.patient_id     = patient.patient_id
LEFT JOIN _enc_pharmacy_27         ON _enc_pharmacy_27.patient_id        = patient.patient_id
LEFT JOIN _enc_vl_sample_67         ON _enc_vl_sample_67.patient_id        = patient.patient_id
LEFT JOIN _enc_eac_69         ON _enc_eac_69.patient_id        = patient.patient_id
LEFT JOIN _enc_otz_73         ON _enc_otz_73.patient_id        = patient.patient_id
LEFT JOIN _inh_last_dispensed    ON _inh_last_dispensed.person_id    = patient.patient_id

-- biometrics
LEFT JOIN (SELECT DISTINCT patient_Id, MIN(date_created) AS date_created FROM biometricinfo GROUP BY patient_Id) AS biometrictable
    ON biometrictable.patient_Id = patient.patient_id AND patient.voided = 0
LEFT JOIN (SELECT DISTINCT patient_Id FROM biometricinfo WHERE template NOT LIKE 'Rk1S%' OR CONVERT(new_template USING utf8) NOT LIKE 'Rk1S%') AS invalidprint
    ON invalidprint.patient_Id = patient.patient_id AND patient.voided = 0

-- facility
LEFT JOIN global_property gp_datim              ON gp_datim.property = 'facility_datim_code'
LEFT JOIN global_property gp_facility              ON gp_facility.property = 'Facility_Name'
LEFT JOIN nigeria_datimcode_mapping        ON gp_datim.property_value = nigeria_datimcode_mapping.datim_code

-- biometric recapture
LEFT JOIN (
    SELECT patient_Id, MAX(date_created) AS RecaptureDate, recapture_count
    FROM biometricverificationinfo GROUP BY patient_Id
) AS bvinfo ON bvinfo.patient_Id = patient.patient_id

-- patient address
LEFT JOIN (
    SELECT a.person_id, CONCAT_WS(', ', NULLIF(TRIM(a.address1),''), NULLIF(TRIM(a.address2),''), NULLIF(TRIM(a.city_village),''), NULLIF(TRIM(a.state_province),'')) AS Address
    FROM person_address AS a WHERE a.voided = 0 GROUP BY a.person_id
) AS addr ON addr.person_id = person.person_id

WHERE patient.voided = 0
  AND pid1.identifier IS NOT NULL

GROUP BY patient.patient_id;


-- ===========================================================================
-- CLEANUP
-- ===========================================================================
DROP TEMPORARY TABLE IF EXISTS _f16_anc_num_165567;  DROP TEMPORARY TABLE IF EXISTS _last_pickup_162240;
DROP TEMPORARY TABLE IF EXISTS _f27_regimen_line_165708;  DROP TEMPORARY TABLE IF EXISTS _f27_next_appt_5096;
DROP TEMPORARY TABLE IF EXISTS _f14_pregnancy_165050;  DROP TEMPORARY TABLE IF EXISTS _f14_next_appt_5096;
DROP TEMPORARY TABLE IF EXISTS _f14_weight_5089;    DROP TEMPORARY TABLE IF EXISTS _f14_tb_status_1659;
DROP TEMPORARY TABLE IF EXISTS _f14_cx_cancer_screen_167139;  DROP TEMPORARY TABLE IF EXISTS _f14_cx_cancer_treat_167150;
DROP TEMPORARY TABLE IF EXISTS _f21_viral_load_856;     DROP TEMPORARY TABLE IF EXISTS _f21_cd4_count_5497;
DROP TEMPORARY TABLE IF EXISTS _f21_sample_date_159951;  DROP TEMPORARY TABLE IF EXISTS _f13_outcome_165470;
DROP TEMPORARY TABLE IF EXISTS _f56_inh_start_164852;  DROP TEMPORARY TABLE IF EXISTS _f56_inh_stop_166096;
DROP TEMPORARY TABLE IF EXISTS _f53_inh_start_165994;  DROP TEMPORARY TABLE IF EXISTS _f53_inh_outcome_166007;
DROP TEMPORARY TABLE IF EXISTS _f53_inh_outcome_date_166008;  DROP TEMPORARY TABLE IF EXISTS _f73_otz_outcome_date_166008;
DROP TEMPORARY TABLE IF EXISTS _pq_pickup_date_162240; DROP TEMPORARY TABLE IF EXISTS _pq_outcome_165470;
DROP TEMPORARY TABLE IF EXISTS _a_art_start_159599;    DROP TEMPORARY TABLE IF EXISTS _a_care_entry_160540;
DROP TEMPORARY TABLE IF EXISTS _a_hiv_confirmed_160554;    DROP TEMPORARY TABLE IF EXISTS _a_transfer_in_date_160534;
DROP TEMPORARY TABLE IF EXISTS _a_transfer_in_status_165242;    DROP TEMPORARY TABLE IF EXISTS _a_returned_to_care_165775;
DROP TEMPORARY TABLE IF EXISTS _a_termination_date_165469;    DROP TEMPORARY TABLE IF EXISTS _a_nok_phone_159635;
DROP TEMPORARY TABLE IF EXISTS _a_ts_phone_160642;    DROP TEMPORARY TABLE IF EXISTS _a_baseline_tb_start_1113;
DROP TEMPORARY TABLE IF EXISTS _a_baseline_tb_stop_159431;    DROP TEMPORARY TABLE IF EXISTS _fo_initial_regimen_line_165708;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_1st_line_164506;   DROP TEMPORARY TABLE IF EXISTS _fo_initial_1st_line_alt_164507;
DROP TEMPORARY TABLE IF EXISTS _fo_initial_2nd_line_164513;   DROP TEMPORARY TABLE IF EXISTS _fo_initial_2nd_line_alt_164514;
DROP TEMPORARY TABLE IF EXISTS _min_cd4;     DROP TEMPORARY TABLE IF EXISTS _last_visit;
DROP TEMPORARY TABLE IF EXISTS _enc_pharmacy_27;         DROP TEMPORARY TABLE IF EXISTS _enc_vl_sample_67;
DROP TEMPORARY TABLE IF EXISTS _enc_eac_69;         DROP TEMPORARY TABLE IF EXISTS _enc_otz_73;
DROP TEMPORARY TABLE IF EXISTS _inh_last_dispensed;
