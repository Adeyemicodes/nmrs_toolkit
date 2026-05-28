-- '----------------------------------------------------     '
-- '  CATHOLIC     CARITAS    FOUNDATION  of   NIGERIA '
-- '----------------------------------------------------     '
-- '     '
-- '   *******     *******    *********   ***      ***   '
-- '  *********   *********   *********   ****     ***   '
-- '  ***   ***   ***   ***   ***         *****    ***   '
-- '  ***         ***         ***         ******   ***   '
-- '  ***         ***         *********   *** ***  ***   '
-- '  ***         ***         *********   ***  *** ***   '
-- '  ***         ***         ***         ***   ******   '
-- '  ***   ***   ***   ***   ***         ***    *****   '
-- '  *********   *********   ***         ***     ****   '
-- '   *******     *******    ***         ***      ***   '
-- '     '
-- '----------------------------------------------------     '
-- '15th November, 2024     '
-- '----------------------------------------------------     '
-- ADH Data pull script.
-- Created 21st July, 2022 by Egbo  Stanley @ Delta State...
-- Edited 15th November, 2024 by A. Danjuma @ Imo State... ;)
-- Modified: 24th October, 2024 by A. Adeyemi @ Imo State... ;)

Use openmrs;

Select 
'CCFN' AS IP, 'Imo' as State,
(SELECT global_property.property_value from global_property where property='Facility_Name') as Facility_Name,
(SELECT global_property.property_value from global_property where property = 'Facility_Datim_Code') as Datim_Code,
pid.identifier AS `ART_ID`, pid2.identifier AS  `Hospital_No`, person.gender AS `Sex`,
DATE_FORMAT(person.birthdate,'%d-%b-%Y') AS `DOB`,
FLOOR(DATEDIFF(CURDATE(), person.birthdate) / 365.25) AS `current_Age`, 
CONCAT(pn.given_name,' ',pn.family_name) AS `Patient_Name`,
CAST(psn_atr.value AS CHAR) AS `Phone_No`,
(SELECT MAX(DATE_FORMAT(value_datetime,'%d-%b-%Y')) from obs WHERE person_id=pid.patient_id and concept_id=159599) as `ART_Start_Date`,

MAX(IF(o.concept_id=165394,o.value_text,NULL)) AS `AHD_LabID`,
MAX(IF(e2.concept_id=5497,e2.value_numeric,NULL)) AS `CD4_Count`,
MAX(IF(e2.concept_id=5497,DATE_FORMAT(e2.obs_datetime,'%d/%m/%Y'),NULL)) AS `CD4_CountDate`,
CASE -- AHD_Indication
    WHEN a.concept_id = 167079 AND a.value_coded = 162080 THEN 'baseline_AHD'
    WHEN a.concept_id = 167079 AND a.value_coded = 162081 THEN 'Repeat_AHD'
ELSE NULL END AS AHD_Indication, 
CASE -- CD4_LFA_RESULT
   WHEN b.concept_id = 167088 AND b.value_coded = 167086 THEN '<200'
   WHEN b.concept_id = 167088 AND b.value_coded = 167087 THEN '>=200'
ELSE NULL END AS CD4_LFA_RESULT,
CASE -- TB_LF_LAM
    WHEN c.concept_id = 166697 AND c.value_coded = 703 THEN 'LF-LAM +ve'
    WHEN c.concept_id = 166697 AND c.value_coded = 664 THEN 'LF-LAM -ve'
ELSE NULL 
END AS TB_LF_LAM,
CASE -- Serology_CrAg
    WHEN d.concept_id = 167090 AND d.value_coded = 703 THEN 'Serology_CrAg +ve'
    WHEN d.concept_id = 167090 AND d.value_coded = 664 THEN 'Serology_CrAg -ve'
ELSE NULL 
END AS Serology_CrAg,
CASE -- CSF_CrAg
    WHEN e1.concept_id = 167082 AND e1.value_coded = 703 THEN 'CSF_CrAg +ve'
    WHEN e1.concept_id = 167082 AND e1.value_coded = 664 THEN 'CSF_CrAg -ve'
ELSE NULL 
END AS CSF_CrAg,

-- WHO Clinical Stage - Get most recent per patient (from any encounter)
(SELECT 
    CASE 
        WHEN value_coded = 1204 THEN 'WHO Stage 1'
        WHEN value_coded = 1205 THEN 'WHO Stage 2'
        WHEN value_coded = 1206 THEN 'WHO Stage 3'
        WHEN value_coded = 1207 THEN 'WHO Stage 4'
        ELSE NULL
    END
 FROM obs 
 WHERE person_id = pid.patient_id 
   AND concept_id = 5356 
   AND voided = 0 
   AND value_coded IS NOT NULL
 ORDER BY obs_datetime DESC 
 LIMIT 1
) AS WHO_Clinical_Stage,

DATE_FORMAT(a.date_created,'%Y-%m-%d') AS Date_Captured

from obs o
left join person on(person.person_id=o.person_id AND o.voided=0)
left join `person_name` pn ON (pn.`person_id`=o.person_id AND o.voided=0 AND pn.`preferred`=1)
left join person_attribute psn_atr ON (o.person_id=psn_atr.person_id and psn_atr.person_attribute_type_id=8)
left join encounter e on (e.encounter_id = o.encounter_id AND e.patient_id = o.person_id)
left join visit v on (v.patient_id = o.person_id AND v.visit_id = e.visit_id)
left join concept_name cn on (o.concept_id=cn.concept_id AND cn.locale='en' AND cn.locale_preferred=1)
left join patient_identifier pid on (pid.patient_id = o.person_id AND pid.identifier_type =4 AND pid.voided =0)
left join patient_identifier pid2 on(pid2.patient_id=o.person_id AND o.voided=0 AND pid2.identifier_type=5 and pid2.voided=0)
left join (select person_id, concept_id, value_coded, date_created, encounter_id, voided from obs where concept_id = 167079) AS a 
on (a.person_id = o.person_id AND a.concept_id = 167079 AND a.voided =0 AND a.encounter_id =e.encounter_id)
left join (select person_id, concept_id, value_coded, obs_datetime, encounter_id, voided from obs where concept_id = 167088) AS b 
on (b.person_id = o.person_id AND b.concept_id = 167088 AND b.voided =0 AND b.encounter_id =e.encounter_id)
left join (select person_id, concept_id, value_coded, obs_datetime, encounter_id, voided from obs where concept_id = 166697) AS c 
on (c.person_id = o.person_id AND c.concept_id = 166697 AND c.voided =0 AND c.encounter_id =e.encounter_id)
left join (select person_id, concept_id, value_coded, obs_datetime, encounter_id, voided from obs where concept_id = 167090) AS d 
on (d.person_id = o.person_id AND d.concept_id = 167090 AND d.voided =0 AND d.encounter_id =e.encounter_id)
left join (select person_id, concept_id, value_coded, obs_datetime, encounter_id, voided from obs where concept_id = 167082) AS e1
on (e1.person_id = o.person_id AND e1.concept_id = 167082 AND e1.voided =0 AND e1.encounter_id =e.encounter_id)
left join (select person_id, concept_id, value_numeric, encounter_id, obs_datetime, voided from obs where concept_id = 5497) AS e2
on (e2.person_id = o.person_id AND e2.concept_id = 5497 AND e2.voided =0 AND e2.encounter_id =e.encounter_id)
where o.voided =0 AND e.encounter_type =11 AND e.form_id =21
group by o.person_id;