USE openmrs;
SET @startdate = '2021-10-01';
SET @enddate = '2022-08-30';

SELECT 
    'CCFN' AS IP,
    'Enugu' AS State,
    (SELECT 
            global_property.property_value
        FROM
            global_property
        WHERE
            property = 'Facility_Name') AS Facility_Name,
    (SELECT 
            global_property.property_value
        FROM
            global_property
        WHERE
            property = 'Facility_Datim_Code') AS Datim_Code,
    pid.identifier AS ART_Number,
    pe.gender AS Sex,
    pe.birthdate AS DOB,
    CONCAT(pn.given_name, ' ', pn.family_name) AS Patient_Name,
    MAX(IF(obs.concept_id = 167079,
        cn1.name,
        NULL)) AS Indication_for_AHD,
    MAX(IF(obs.concept_id = 165731, 'Yes', 'No')) AS `CD4+ Order`,
    MAX(IF(obs.concept_id = 5497,
        obs.value_numeric,
        NULL)) AS `CD4 Count`,
    MAX(IF(obs.concept_id = 167085, 'Yes', 'No')) AS `CD4 LFA Order`,
    MAX(IF(obs.concept_id = 167088,
        IF(obs.value_coded = 167086,
            '<200',
            '>=200'),
        NULL)) AS `CD4 LFA Result`,
    MAX(IF(obs.concept_id = 167080, 'Yes', 'No')) AS `TB LF-LAM Order`,
    MAX(IF(obs.concept_id = 166697,
        cn1.name,
        NULL)) AS `TB LF-LAM Result`,
    MAX(IF(obs.concept_id = 167089, 'Yes', 'No')) AS `Sereology for CrAg Order`,
    MAX(IF(obs.concept_id = 167090,
        cn1.name,
        NULL)) AS `Sereology for CrAg Result`,
    MAX(IF(obs.concept_id = 167081, 'Yes', 'No')) AS `CSF for CrAg Order`,
    MAX(IF(obs.concept_id = 167082,
        cn1.name,
        NULL)) AS `CSF for CrAg Result`,
    e.encounter_datetime AS `Visit Date`
FROM
    patient pa
        JOIN
    person pe ON pa.patient_id = pe.person_id
        JOIN
    person_name pn ON pa.patient_id = pn.person_id
        JOIN
    patient_identifier pid ON pa.patient_id = pid.patient_id
        JOIN
    obs ob ON ob.person_id = pa.patient_id
        JOIN
    encounter e ON e.patient_id = pa.patient_id
        JOIN
    obs ON obs.person_id = pa.patient_id
        LEFT JOIN
    concept_name cn ON obs.concept_id = cn.concept_id
        LEFT JOIN
    concept_name cn1 ON obs.value_coded = cn1.concept_id
WHERE
    pa.voided = 0 AND obs.voided = 0 and e.voided = 0
        AND pid.identifier_type = 4
        AND obs.encounter_id = e.encounter_id
        AND e.encounter_datetime BETWEEN @startdate AND @enddate
        AND obs.concept_id IN (167079 , 165731,
        5497,
        167080,
        166697,
        167089,
        167090,
        167088,
        165731,
        167085)
        AND cn1.locale = 'en'
GROUP BY pid.identifier;