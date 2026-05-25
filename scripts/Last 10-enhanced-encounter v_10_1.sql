/* 
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 * ╔═══════════════════════════════════════════════════════════════════════════════════════════════════════════╗
 * ║         				Enhanced Patient VL and Refill History SQL Script - Last 10 Encounters             ║
 * ╚═══════════════════════════════════════════════════════════════════════════════════════════════════════════╝
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 * 
 * Description:  Comprehensive HIV patient monitoring report consolidating demographics,
 *				 ART treatment history, medication adherence, viral suppression tracking, 
 * 				 and tuberculosis preventive therapy (TPT) monitoring into a single dataset.
 * 
 * Version:      10.0.1
 * Created:      October 21, 2024
 * Updated:	 	 April 1, 2026
 * Author:	 	 Adeyemi Adegoke
 * 
 * ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 * │                               🏥 Property of Caritas Nigeria                                    	     │
 * │                          Developed for Healthcare Data Excellence                                       │
 * └─────────────────────────────────────────────────────────────────────────────────────────────────────────┘	
 * 
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 */
 
use openmrs;

SET @start_date = '2020-10-01';
SET @end_date = '2026-12-31';
SET @current_date = CURDATE();

SELECT 
    pi.identifier AS ART_ID,
    
    /* Demographics */
    p.gender AS Sex,
    p.birthdate AS DOB,
    TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) AS Current_Age,
    
    /* Age Band in 5-year increments */
    CASE
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 0 AND 4 THEN '0-4'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 5 AND 9 THEN '5-9'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 10 AND 14 THEN '10-14'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 15 AND 19 THEN '15-19'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 20 AND 24 THEN '20-24'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 25 AND 29 THEN '25-29'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 30 AND 34 THEN '30-34'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 35 AND 39 THEN '35-39'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 40 AND 44 THEN '40-44'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 45 AND 49 THEN '45-49'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 50 AND 54 THEN '50-54'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 55 AND 59 THEN '55-59'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) BETWEEN 60 AND 64 THEN '60-64'
        WHEN TIMESTAMPDIFF(YEAR, p.birthdate, @current_date) >= 65 THEN '65+'
        ELSE 'Unknown'
    END AS Age_Band,
    
    /* ART Start Date */
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 159599 
        AND o.voided = 0
    ) AS ART_Start_Date,
    
    /* TPT dates from different forms */
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 164852
        AND o.voided = 0
    ) AS ART_Com_Form_TPT_Start_Date,
    
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 166096
        AND o.voided = 0
    ) AS ART_Com_Form_TPT_Stop_Date,
    
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 165994
        AND o.voided = 0
    ) AS IPT_Form_TPT_Start_Date,
    
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 166008
        AND o.voided = 0
    ) AS IPT_Form_TPT_Outcome_Date,
    
    /* TPT Outcome (coded value) */
    (SELECT 
        cn.name
     FROM 
        obs o
        JOIN concept_name cn ON o.value_coded = cn.concept_id AND cn.locale = 'en' AND cn.voided = 0
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 166008
        AND o.voided = 0
     ORDER BY
        o.obs_datetime DESC
     LIMIT 1
    ) AS IPT_Form_TPT_Outcome,
    
    /* TB Confirmation Status and Date */
(SELECT 
    'Yes'
 FROM 
    obs o
    JOIN encounter e ON o.encounter_id = e.encounter_id
 WHERE 
    o.person_id = pi.patient_id 
    AND o.concept_id = 1659
    AND o.value_coded = 1661
    AND e.encounter_type = 12
    AND e.form_id = 14
    AND o.voided = 0
    AND e.voided = 0
    AND DATE(o.obs_datetime) BETWEEN @start_date AND @end_date
 ORDER BY o.obs_datetime DESC
 LIMIT 1
) AS Ccard_TB_Confirmed,

(SELECT 
    DATE(o.obs_datetime)
 FROM 
    obs o
    JOIN encounter e ON o.encounter_id = e.encounter_id
 WHERE 
    o.person_id = pi.patient_id 
    AND o.concept_id = 1659
    AND o.value_coded = 1661
    AND e.encounter_type = 12
    AND e.form_id = 14
    AND o.voided = 0
    AND e.voided = 0
    AND DATE(o.obs_datetime) BETWEEN @start_date AND @end_date
 ORDER BY o.obs_datetime DESC
 LIMIT 1
) AS Ccard_TB_Confirmed_Date,

/* Date Difference Calculations */
DATEDIFF(
    (SELECT 
        DATE(o.obs_datetime)
     FROM 
        obs o
        JOIN encounter e ON o.encounter_id = e.encounter_id
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 1659
        AND o.value_coded = 1661
        AND e.encounter_type = 12
        AND e.form_id = 14
        AND o.voided = 0
        AND e.voided = 0
        AND DATE(o.obs_datetime) BETWEEN @start_date AND @end_date
     ORDER BY o.obs_datetime DESC
     LIMIT 1
    ),
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 166096
        AND o.voided = 0
    )
) AS Days_TB_Confirmed_To_ART_TPT_Stop,

DATEDIFF(
    (SELECT 
        DATE(o.obs_datetime)
     FROM 
        obs o
        JOIN encounter e ON o.encounter_id = e.encounter_id
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 1659
        AND o.value_coded = 1661
        AND e.encounter_type = 12
        AND e.form_id = 14
        AND o.voided = 0
        AND e.voided = 0
        AND DATE(o.obs_datetime) BETWEEN @start_date AND @end_date
     ORDER BY o.obs_datetime DESC
     LIMIT 1
    ),
    (SELECT 
        DATE(MAX(o.value_datetime))
     FROM 
        obs o 
     WHERE 
        o.person_id = pi.patient_id 
        AND o.concept_id = 166008
        AND o.voided = 0
    )
) AS Days_TB_Confirmed_To_IPT_Outcome,
    
    /* Calculated Status - Pure calculation from refill data */
    CASE
        WHEN r.refill_date IS NOT NULL AND r.refill_duration IS NOT NULL
        THEN
            CASE
                WHEN DATEDIFF(
                    DATE_ADD(r.refill_date, INTERVAL (r.refill_duration + 28) DAY),
                    @current_date
                ) >= 0 
                THEN 'Active'
                ELSE 'Inactive'
            END
        ELSE NULL
    END AS Calculated_Status,
    
    /* Documented Status - From concept_id 165470 */
    c.status_name AS Documented_Status,
    
    /* Status Date - Documented status date if exists, otherwise most recent refill date */
    COALESCE(c.status_date, r.refill_date) AS Status_Date,
    
    /* Refill dates (Last 10) */
    MAX(CASE WHEN r2.rn = 1 THEN r2.refill_date END) AS refill_date_1,
    MAX(CASE WHEN r2.rn = 2 THEN r2.refill_date END) AS refill_date_2,
    MAX(CASE WHEN r2.rn = 3 THEN r2.refill_date END) AS refill_date_3,
    MAX(CASE WHEN r2.rn = 4 THEN r2.refill_date END) AS refill_date_4,
    MAX(CASE WHEN r2.rn = 5 THEN r2.refill_date END) AS refill_date_5,
    MAX(CASE WHEN r2.rn = 6 THEN r2.refill_date END) AS refill_date_6,
    MAX(CASE WHEN r2.rn = 7 THEN r2.refill_date END) AS refill_date_7,
    MAX(CASE WHEN r2.rn = 8 THEN r2.refill_date END) AS refill_date_8,
    MAX(CASE WHEN r2.rn = 9 THEN r2.refill_date END) AS refill_date_9,
    MAX(CASE WHEN r2.rn = 10 THEN r2.refill_date END) AS refill_date_10,
    
    /* Refill durations (Last 10) */
    MAX(CASE WHEN r2.rn = 1 THEN r2.refill_duration END) AS refill_duration_1,
    MAX(CASE WHEN r2.rn = 2 THEN r2.refill_duration END) AS refill_duration_2,
    MAX(CASE WHEN r2.rn = 3 THEN r2.refill_duration END) AS refill_duration_3,
    MAX(CASE WHEN r2.rn = 4 THEN r2.refill_duration END) AS refill_duration_4,
    MAX(CASE WHEN r2.rn = 5 THEN r2.refill_duration END) AS refill_duration_5,
    MAX(CASE WHEN r2.rn = 6 THEN r2.refill_duration END) AS refill_duration_6,
    MAX(CASE WHEN r2.rn = 7 THEN r2.refill_duration END) AS refill_duration_7,
    MAX(CASE WHEN r2.rn = 8 THEN r2.refill_duration END) AS refill_duration_8,
    MAX(CASE WHEN r2.rn = 9 THEN r2.refill_duration END) AS refill_duration_9,
    MAX(CASE WHEN r2.rn = 10 THEN r2.refill_duration END) AS refill_duration_10,
    
    /* Viral load sample dates (Last 10) */
    MAX(CASE WHEN v.rn = 1 THEN v.vl_sample_date END) AS vl_sample_date_1,
    MAX(CASE WHEN v.rn = 2 THEN v.vl_sample_date END) AS vl_sample_date_2,
    MAX(CASE WHEN v.rn = 3 THEN v.vl_sample_date END) AS vl_sample_date_3,
    MAX(CASE WHEN v.rn = 4 THEN v.vl_sample_date END) AS vl_sample_date_4,
    MAX(CASE WHEN v.rn = 5 THEN v.vl_sample_date END) AS vl_sample_date_5,
    MAX(CASE WHEN v.rn = 6 THEN v.vl_sample_date END) AS vl_sample_date_6,
    MAX(CASE WHEN v.rn = 7 THEN v.vl_sample_date END) AS vl_sample_date_7,
    MAX(CASE WHEN v.rn = 8 THEN v.vl_sample_date END) AS vl_sample_date_8,
    MAX(CASE WHEN v.rn = 9 THEN v.vl_sample_date END) AS vl_sample_date_9,
    MAX(CASE WHEN v.rn = 10 THEN v.vl_sample_date END) AS vl_sample_date_10,
    
    /* Viral load results (Last 10) */
    MAX(CASE WHEN v.rn = 1 THEN v.vl_result END) AS vl_result_1,
    MAX(CASE WHEN v.rn = 2 THEN v.vl_result END) AS vl_result_2,
    MAX(CASE WHEN v.rn = 3 THEN v.vl_result END) AS vl_result_3,
    MAX(CASE WHEN v.rn = 4 THEN v.vl_result END) AS vl_result_4,
    MAX(CASE WHEN v.rn = 5 THEN v.vl_result END) AS vl_result_5,
    MAX(CASE WHEN v.rn = 6 THEN v.vl_result END) AS vl_result_6,
    MAX(CASE WHEN v.rn = 7 THEN v.vl_result END) AS vl_result_7,
    MAX(CASE WHEN v.rn = 8 THEN v.vl_result END) AS vl_result_8,
    MAX(CASE WHEN v.rn = 9 THEN v.vl_result END) AS vl_result_9,
    MAX(CASE WHEN v.rn = 10 THEN v.vl_result END) AS vl_result_10,
    
    /* Viral load result dates (Last 10) */
    MAX(CASE WHEN v.rn = 1 THEN v.vl_result_date END) AS vl_result_date_1,
    MAX(CASE WHEN v.rn = 2 THEN v.vl_result_date END) AS vl_result_date_2,
    MAX(CASE WHEN v.rn = 3 THEN v.vl_result_date END) AS vl_result_date_3,
    MAX(CASE WHEN v.rn = 4 THEN v.vl_result_date END) AS vl_result_date_4,
    MAX(CASE WHEN v.rn = 5 THEN v.vl_result_date END) AS vl_result_date_5,
    MAX(CASE WHEN v.rn = 6 THEN v.vl_result_date END) AS vl_result_date_6,
    MAX(CASE WHEN v.rn = 7 THEN v.vl_result_date END) AS vl_result_date_7,
    MAX(CASE WHEN v.rn = 8 THEN v.vl_result_date END) AS vl_result_date_8,
    MAX(CASE WHEN v.rn = 9 THEN v.vl_result_date END) AS vl_result_date_9,
    MAX(CASE WHEN v.rn = 10 THEN v.vl_result_date END) AS vl_result_date_10,
    
    /* Current Line Regimen (Last 10) */
    MAX(CASE WHEN r2.rn = 1 THEN line.regimen_line_name END) AS current_line_reg_1,
    MAX(CASE WHEN r2.rn = 2 THEN line.regimen_line_name END) AS current_line_reg_2,
    MAX(CASE WHEN r2.rn = 3 THEN line.regimen_line_name END) AS current_line_reg_3,
    MAX(CASE WHEN r2.rn = 4 THEN line.regimen_line_name END) AS current_line_reg_4,
    MAX(CASE WHEN r2.rn = 5 THEN line.regimen_line_name END) AS current_line_reg_5,
    MAX(CASE WHEN r2.rn = 6 THEN line.regimen_line_name END) AS current_line_reg_6,
    MAX(CASE WHEN r2.rn = 7 THEN line.regimen_line_name END) AS current_line_reg_7,
    MAX(CASE WHEN r2.rn = 8 THEN line.regimen_line_name END) AS current_line_reg_8,
    MAX(CASE WHEN r2.rn = 9 THEN line.regimen_line_name END) AS current_line_reg_9,
    MAX(CASE WHEN r2.rn = 10 THEN line.regimen_line_name END) AS current_line_reg_10,
    
    /* Current Regimen (Last 10) */
    MAX(CASE WHEN r2.rn = 1 THEN reg.regimen_name END) AS current_regimen_1,
    MAX(CASE WHEN r2.rn = 2 THEN reg.regimen_name END) AS current_regimen_2,
    MAX(CASE WHEN r2.rn = 3 THEN reg.regimen_name END) AS current_regimen_3,
    MAX(CASE WHEN r2.rn = 4 THEN reg.regimen_name END) AS current_regimen_4,
    MAX(CASE WHEN r2.rn = 5 THEN reg.regimen_name END) AS current_regimen_5,
    MAX(CASE WHEN r2.rn = 6 THEN reg.regimen_name END) AS current_regimen_6,
    MAX(CASE WHEN r2.rn = 7 THEN reg.regimen_name END) AS current_regimen_7,
    MAX(CASE WHEN r2.rn = 8 THEN reg.regimen_name END) AS current_regimen_8,
    MAX(CASE WHEN r2.rn = 9 THEN reg.regimen_name END) AS current_regimen_9,
    MAX(CASE WHEN r2.rn = 10 THEN reg.regimen_name END) AS current_regimen_10

FROM patient_identifier pi

/* Join person table for sex and DOB */
LEFT JOIN person p ON pi.patient_id = p.person_id AND p.voided = 0

/* Get most recent refill information for status calculation - SIMPLIFIED */
LEFT JOIN (
    SELECT 
        e.patient_id,
        DATE(e.encounter_datetime) AS refill_date,
        o1.value_numeric AS refill_duration
    FROM encounter e
    JOIN obs o1 ON e.encounter_id = o1.encounter_id
    JOIN obs o2 ON e.encounter_id = o2.encounter_id AND o1.obs_group_id = o2.obs_group_id
    JOIN (
        SELECT 
            patient_id,
            MAX(encounter_datetime) AS max_encounter_datetime
        FROM encounter
        WHERE encounter_type = 13
        AND voided = 0
        AND DATE(encounter_datetime) BETWEEN @start_date AND @end_date
        GROUP BY patient_id
    ) max_enc ON e.patient_id = max_enc.patient_id AND e.encounter_datetime = max_enc.max_encounter_datetime
    WHERE e.encounter_type = 13
    AND e.voided = 0
    AND o1.concept_id = 159368
    AND o2.concept_id = 165724
    AND o1.voided = 0
    AND o2.voided = 0
    AND DATE(e.encounter_datetime) BETWEEN @start_date AND @end_date
) r ON pi.patient_id = r.patient_id

/* Get current status information */
LEFT JOIN (
    SELECT 
        o.person_id,
        o.obs_datetime AS status_date,
        cn.name AS status_name
    FROM obs o
    JOIN concept_name cn ON o.value_coded = cn.concept_id
    JOIN (
        SELECT 
            person_id,
            MAX(obs_datetime) AS latest_status_date
        FROM obs
        WHERE concept_id = 165470
        AND voided = 0
        GROUP BY person_id
    ) latest ON o.person_id = latest.person_id AND o.obs_datetime = latest.latest_status_date
    WHERE o.concept_id = 165470
    AND o.voided = 0
    AND cn.locale = 'en'
    AND cn.voided = 0
) c ON pi.patient_id = c.person_id

/* Get ranked refill information with regimen data */
LEFT JOIN (
    SELECT 
        t.*,
        @rn := IF(@prev_pid = t.patient_id, @rn + 1, 1) AS rn,
        @prev_pid := t.patient_id
    FROM (
        SELECT 
            e.patient_id,
            e.encounter_id,
            DATE(e.encounter_datetime) AS refill_date,
            o1.value_numeric AS refill_duration
        FROM encounter e
        JOIN obs o1 ON e.encounter_id = o1.encounter_id
        JOIN obs o2 ON e.encounter_id = o2.encounter_id AND o1.obs_group_id = o2.obs_group_id
        WHERE e.encounter_type = 13
        AND e.voided = 0
        AND o1.concept_id = 159368
        AND o2.concept_id = 165724
        AND o1.voided = 0
        AND o2.voided = 0
        AND e.encounter_datetime BETWEEN @start_date AND @end_date
        ORDER BY e.patient_id, e.encounter_datetime DESC
    ) t
    CROSS JOIN (SELECT @rn := 0, @prev_pid := 0) vars
) r2 ON pi.patient_id = r2.patient_id AND r2.rn <= 10

/* Get regimen line information */
LEFT JOIN (
    SELECT 
        o.encounter_id,
        cn.name AS regimen_line_name
    FROM obs o
    JOIN concept_name cn ON o.value_coded = cn.concept_id
    WHERE o.concept_id = 165708
    AND o.voided = 0
    AND cn.voided = 0
    AND cn.locale = 'en'
) line ON r2.encounter_id = line.encounter_id

/* Get current regimen information */
LEFT JOIN (
    SELECT 
        o.encounter_id,
        cn.name AS regimen_name
    FROM obs o
    JOIN concept_name cn ON o.value_coded = cn.concept_id
    WHERE o.concept_id IN (164506, 164513, 164507, 165702, 164514)
    AND o.voided = 0
    AND cn.voided = 0
    AND cn.locale = 'en'
) reg ON r2.encounter_id = reg.encounter_id

/* Get viral load information */
LEFT JOIN (
    SELECT 
        t.*,
        @rn := IF(@prev_pid = t.patient_id, @rn + 1, 1) AS rn,
        @prev_pid := t.patient_id
    FROM (
        SELECT 
            e.patient_id,
            e.encounter_id,
            MAX(CASE WHEN o.concept_id = 159951 THEN DATE(o.value_datetime) END) AS vl_sample_date,
            MAX(CASE WHEN o.concept_id = 856 THEN o.value_numeric END) AS vl_result,
            MAX(CASE WHEN o.concept_id = 166423 THEN DATE(o.value_datetime) END) AS vl_result_date
        FROM encounter e
        JOIN obs o ON e.encounter_id = o.encounter_id
        WHERE e.encounter_type = 11
        AND e.voided = 0
        AND o.voided = 0
        AND e.encounter_datetime BETWEEN @start_date AND @end_date
        GROUP BY e.encounter_id
        ORDER BY e.patient_id, e.encounter_datetime DESC
    ) t
    CROSS JOIN (SELECT @rn := 0, @prev_pid := 0) vars
) v ON pi.patient_id = v.patient_id AND v.rn <= 10

WHERE pi.identifier_type = 4
AND pi.voided = 0
GROUP BY pi.identifier;
