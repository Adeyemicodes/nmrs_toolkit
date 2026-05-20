-- EAC Data Pull Script
-- Pulls data for Enhanced Adherence Counselling (EAC) based on lab results and EAC sessions
-- Date: August 18, 2025
-- Written by caritas Health Informatics @DevTeam @JtechAutomation 

USE openmrs;

SET GLOBAL sql_mode = '';


DROP FUNCTION IF EXISTS `getdatevalueobsid`;
DROP FUNCTION IF EXISTS `getmaxconceptobsid`;
DROP FUNCTION IF EXISTS `getcodedvalueobsid`;
DROP FUNCTION IF EXISTS `getmaxconceptobsidwithformid`;
DROP FUNCTION IF EXISTS `getoutcome`;
DROP FUNCTION IF EXISTS `getobsdatetime`;
DROP FUNCTION IF EXISTS `getconceptval`;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getconceptval`(`obsid` int,`cid` int, pid int) RETURNS decimal(10,0)
BEGIN
	
   DECLARE value_num INT;
    SELECT obs.value_numeric into value_num from obs WHERE  obs.obs_group_id is not null and obs.obs_group_id=obsid and obs.concept_id=cid and obs.person_id=pid and obs.voided=0 limit 1;
	RETURN value_num;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getobsdatetime`(`obsid` int) RETURNS date
BEGIN
    DECLARE val DATE;
    SELECT  obs.obs_datetime INTO val from obs WHERE  obs.obs_id=obsid;
	RETURN val;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getdatevalueobsid`(`obsid` int) RETURNS date
BEGIN
    DECLARE val DATE;
    SELECT  obs.value_datetime into val from obs WHERE  obs.obs_id=obsid;
	RETURN val;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getmaxconceptobsid`(`patientid` int,`conceptid` int, `cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN
    DECLARE value_num INT;
    SELECT  obs.obs_id into value_num from obs WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;
	RETURN value_num;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getcodedvalueobsid`(`obsid` int) RETURNS text CHARSET utf8
BEGIN
    DECLARE val TEXT;
    SELECT  cn.name INTO val from 
		obs 
		inner join concept_name cn on(obs.value_coded=cn.concept_id and cn.locale='en' and cn.locale_preferred=1) where obs.obs_id=obsid;
	RETURN val;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getmaxconceptobsidwithformid`(`patientid` int,`conceptid` int, `formid` int,`cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN
    DECLARE value_num INT;
    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  encounter.form_id=formid and obs.person_id=patientid 
		and obs.concept_id=conceptid and obs.voided=0 and obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;
	RETURN value_num;
END ;;

DELIMITER ;;
CREATE DEFINER=`root`@`localhost` FUNCTION `getoutcome`(`Pharmacy_LastPickupdate` date,`daysofarvrefill` numeric,`LTFUdays` numeric, `today_date` date) RETURNS text CHARSET utf8
BEGIN
DECLARE  LTFUdate DATE;
DECLARE  LTFUnumber NUMERIC;
DECLARE  daysdiff NUMERIC;
DECLARE outcome text;

SET LTFUnumber=daysofarvrefill+LTFUdays;   
SELECT DATE_ADD(Pharmacy_LastPickupdate, INTERVAL LTFUnumber DAY) INTO LTFUdate;  
SELECT DATEDIFF(LTFUdate,today_date) into daysdiff; 
SELECT IF(daysdiff >=0,"Active","LTFU") into outcome;

RETURN outcome;
END ;;

DELIMITER ;

-- Set date parameters (adjust as needed)
SET @startDate = '2024-05-01';
SET @endDate = now();

-- Clean up existing tables
DROP TABLE IF EXISTS ALL_Lab_temp;
DROP TABLE IF EXISTS LatestLab_temp;
DROP TEMPORARY TABLE IF EXISTS EACSessions;
DROP TEMPORARY TABLE IF EXISTS SubsequentLab;
DROP TEMPORARY TABLE IF EXISTS PatientDetails;
DROP TEMPORARY TABLE IF EXISTS FacilityDetails;


CREATE TABLE ALL_Lab_temp (
    person_id INT NOT NULL,
    encounter_id INT NOT NULL,
    encounter_date DATETIME,
    Sample_Collection_Date DATETIME,
    VL_Result_Date DATETIME,
    Date_Result_Received DATETIME,
    Effective_Result_Date DATETIME, 
    VL_Result DOUBLE,
    KEY idx_person_sample_date (person_id, Sample_Collection_Date),
    KEY idx_person_effective_date (person_id, Effective_Result_Date), 
    KEY idx_sample_date (Sample_Collection_Date)
) ENGINE=InnoDB;


INSERT INTO ALL_Lab_temp
SELECT 
    o.person_id,
    e.encounter_id,
    e.encounter_datetime AS encounter_date,
    MAX(CASE WHEN o.concept_id = 159951 THEN o.value_datetime END) AS Sample_Collection_Date,
    MAX(CASE WHEN o.concept_id = 166423 THEN o.value_datetime END) AS VL_Result_Date,
    MAX(CASE WHEN o.concept_id = 165987 THEN o.value_datetime END) AS Date_Result_Received,
  
    COALESCE(
        MAX(CASE WHEN o.concept_id = 166423 THEN o.value_datetime END),
        MAX(CASE WHEN o.concept_id = 165987 THEN o.value_datetime END)
    ) AS Effective_Result_Date,
    MAX(CASE WHEN o.concept_id = 856 THEN o.value_numeric END) AS VL_Result
FROM obs o
    INNER JOIN encounter e ON o.encounter_id = e.encounter_id
WHERE e.form_id = 21
    AND e.encounter_type = 11
    AND o.concept_id IN (159951, 166423, 165987, 856)
    AND e.encounter_datetime BETWEEN @startDate AND @endDate
    AND o.voided = 0
    AND e.voided = 0
GROUP BY o.person_id, e.encounter_id
HAVING MAX(CASE WHEN o.concept_id = 856 THEN o.value_numeric END) IS NOT NULL;


CREATE TABLE LatestLab_temp (
    person_id INT NOT NULL,
    encounter_id INT NOT NULL,
    encounter_date DATETIME,
    Sample_Collection_Date DATETIME,
    VL_Result_Date DATETIME,
    Date_Result_Received DATETIME,
    Effective_Result_Date DATETIME, -- NEW: Computed effective date
    VL_Result DOUBLE,
    PRIMARY KEY (person_id, encounter_id),
    KEY idx_person_effective_date (person_id, Effective_Result_Date), -- NEW INDEX
    KEY idx_effective_result_date (Effective_Result_Date)
) ENGINE=InnoDB;


INSERT INTO LatestLab_temp
SELECT *
FROM ALL_Lab_temp
WHERE VL_Result > 50;


CREATE TEMPORARY TABLE EACSessions (
    person_id INT NOT NULL,
    lab_encounter_id INT NOT NULL,
    encounter_date DATETIME,
    First_EAC DATETIME,
    First_EAC_EntryDate DATETIME,
    Second_EAC DATETIME,
    Second_EAC_EntryDate DATETIME,
    Third_EAC DATETIME,
    Third_EAC_EntryDate DATETIME,
    Extended_EAC DATETIME,
    Extended_EAC_encounter_id INT, -- Debug field
    Barriers_to_Adherence VARCHAR(1000),
    PRIMARY KEY (person_id, lab_encounter_id),
    KEY idx_person_first_eac (person_id, First_EAC)
) ENGINE=InnoDB;

INSERT INTO EACSessions
SELECT 
    ll.person_id,
    ll.encounter_id AS lab_encounter_id,
    ll.encounter_date,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165643 THEN e.encounter_datetime END) AS First_EAC,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165643 THEN o.date_created END) AS First_EAC_EntryDate,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165644 THEN e.encounter_datetime END) AS Second_EAC,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165644 THEN o.date_created END) AS Second_EAC_EntryDate,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165645 THEN e.encounter_datetime END) AS Third_EAC,
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 165645 THEN o.date_created END) AS Third_EAC_EntryDate,
   
    MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 5622 THEN e.encounter_datetime END) AS Extended_EAC,
	MAX(CASE WHEN o.concept_id = 166097 AND o.value_coded = 5622 THEN e.encounter_id END) AS Extended_EAC_encounter_id,
    GROUP_CONCAT(
        DISTINCT CASE WHEN o.concept_id = 165457 THEN 
            CASE o.value_coded 
                WHEN 160587 THEN 'Forgot'
                WHEN 165231 THEN 'Knowledge/ beliefs'
                WHEN 165594 THEN 'Side Effects'
                WHEN 137793 THEN 'Physical Illness'
                WHEN 160246 THEN 'Substance use'
                WHEN 119537 THEN 'Depression'
                WHEN 160588 THEN 'Pill Burden'
                WHEN 165456 THEN 'Transport'
                WHEN 165441 THEN 'Child behavior/refusing'
                WHEN 165359 THEN 'Scheduling'
                WHEN 165233 THEN 'Fear Disclosure'
                WHEN 163316 THEN 'Family/partner'
                WHEN 165451 THEN 'Drug stock out'
                WHEN 165453 THEN 'Stigma'
                WHEN 160584 THEN 'Lost/ran out'
               
                WHEN 5622 THEN 'Other'
                ELSE CONCAT('Unknown:', o.value_coded)
            END 
        END
        ORDER BY o.obs_datetime
        SEPARATOR ', '
    ) AS Barriers_to_Adherence
FROM LatestLab_temp ll
    LEFT JOIN encounter e ON e.patient_id = ll.person_id
        AND e.form_id = 69
        AND e.encounter_type = 32
        
        AND e.encounter_datetime >= ll.Effective_Result_Date
        AND e.encounter_datetime < COALESCE(
            (SELECT MIN(ll2.Effective_Result_Date) 
             FROM LatestLab_temp ll2
             WHERE ll2.person_id = ll.person_id
               AND ll2.Effective_Result_Date > ll.Effective_Result_Date),
            DATE_ADD(@endDate, INTERVAL 1 DAY)
        )
        AND e.voided = 0
    LEFT JOIN obs o ON o.encounter_id = e.encounter_id
        AND o.concept_id IN (166097, 165457) -- EAC session type OR barriers
        AND (
            (o.concept_id = 166097 AND o.value_coded IN (165643, 165644, 165645, 5622)) OR
            (o.concept_id = 165457 AND o.value_coded IN (160587,165231,165594,137793,160246,119537,160588,
            165456,165441,165359,165233,163316,165451,165453,160584,5622))
        )
        AND o.voided = 0
GROUP BY ll.person_id, ll.encounter_id;


CREATE TEMPORARY TABLE SubsequentLab (
    person_id INT NOT NULL,
    lab_encounter_id INT NOT NULL,
    Subsequent_Retest_Date DATETIME,
    subsequent_vl DOUBLE,
    subsequent_VL_Result_Date DATETIME,
    subsequent_Date_Result_Received DATETIME,
    PRIMARY KEY (person_id, lab_encounter_id)
) ENGINE=InnoDB;

-- SIMPLE FIX: Add GROUP BY to prevent duplicates
-- This is the cleanest solution for the duplicate key error

INSERT INTO SubsequentLab
SELECT 
    ll1.person_id,
    ll1.encounter_id AS lab_encounter_id,
    MIN(all2.Sample_Collection_Date) AS Subsequent_Retest_Date,
    MIN(all2.VL_Result) AS subsequent_vl,
    MIN(all2.VL_Result_Date) AS subsequent_VL_Result_Date,
    MIN(all2.Date_Result_Received) AS subsequent_Date_Result_Received
FROM LatestLab_temp ll1
    LEFT JOIN ALL_Lab_temp all2 ON ll1.person_id = all2.person_id
        AND all2.Sample_Collection_Date = (
            SELECT MIN(all3.Sample_Collection_Date)
            FROM ALL_Lab_temp all3
            WHERE all3.person_id = ll1.person_id
              AND all3.Sample_Collection_Date > ll1.Sample_Collection_Date
        )
GROUP BY ll1.person_id, ll1.encounter_id;

CREATE TEMPORARY TABLE PatientDetails (
    person_id INT NOT NULL PRIMARY KEY,
    Client_Name VARCHAR(255),
    ART_ID VARCHAR(50),
    Hospital_Number VARCHAR(50),
    Phone_Number VARCHAR(50),
    Current_Age INT,
    ART_START_DATE VARCHAR(20),
    CurrentARTStatus VARCHAR(50),
    KEY idx_person_id (person_id)
) ENGINE=InnoDB;

INSERT INTO PatientDetails
SELECT 
    p.person_id,
    CONCAT(COALESCE(pn.given_name, ''), ' ', COALESCE(pn.family_name, '')) AS Client_Name,
    pi.identifier AS ART_ID,
    pi2.identifier AS Hospital_Number,
    pa.value AS Phone_Number,
    TIMESTAMPDIFF(YEAR, p.birthdate, CURDATE()) AS Current_Age,
    DATE_FORMAT(getdatevalueobsid(getmaxconceptobsid(p.person_id, 159599, @endDate)), '%d-%b-%Y') AS ART_START_DATE,
    COALESCE(
        getcodedvalueobsid(getmaxconceptobsidwithformid(p.person_id, 165470, 13, @endDate)),
        getoutcome(
            getobsdatetime(getmaxconceptobsidwithformid(p.person_id, 162240, 27, @endDate)),
            getconceptval(getmaxconceptobsidwithformid(p.person_id, 162240, 27, @endDate), 159368, p.person_id),
            28,
            IF(@endDate IS NULL OR @endDate = '', CURDATE(), @endDate)
        )
    ) AS CurrentARTStatus
FROM person p
    INNER JOIN LatestLab_temp ll ON p.person_id = ll.person_id
    LEFT JOIN person_name pn ON p.person_id = pn.person_id AND pn.voided = 0 AND pn.preferred = 1
    LEFT JOIN (
        SELECT patient_id, identifier, date_created, voided
        FROM patient_identifier
        WHERE identifier_type = 4 AND voided = 0
        GROUP BY patient_id
        HAVING date_created = MIN(date_created)
    ) pi ON p.person_id = pi.patient_id
    LEFT JOIN (
        SELECT patient_id, identifier, date_created
        FROM patient_identifier pi2_inner
        WHERE identifier_type = 5 AND voided = 0
        GROUP BY patient_id
        HAVING date_created = MIN(date_created)
    ) pi2 ON p.person_id = pi2.patient_id
    LEFT JOIN (
        SELECT person_id, value, date_created
        FROM person_attribute pa_inner
        WHERE person_attribute_type_id = 8 AND voided = 0
        GROUP BY person_id
        HAVING date_created = MIN(date_created)
    ) pa ON p.person_id = pa.person_id
GROUP BY p.person_id;


CREATE TEMPORARY TABLE FacilityDetails (
    Facility_Name VARCHAR(255),
    Datim_Code VARCHAR(100)
) ENGINE=InnoDB;

INSERT INTO FacilityDetails
SELECT 
    (SELECT property_value FROM global_property WHERE property = 'Facility_Name') AS Facility_Name,
    (SELECT property_value FROM global_property WHERE property = 'facility_Datim_Code') AS Datim_Code;

SELECT 
    fd.Facility_Name,
    fd.Datim_Code,
    pd.Client_Name,
    pd.ART_ID,
    pd.Hospital_Number,
    pd.Phone_Number,
    pd.Current_Age,
    pd.ART_START_DATE,
    pd.CurrentARTStatus,
    DATE_FORMAT(ll.Sample_Collection_Date, '%d-%b-%Y') AS Sample_Collection_Date,
    DATE_FORMAT(ll.VL_Result_Date, '%d-%b-%Y') AS VL_Result_Date,
    DATE_FORMAT(ll.Date_Result_Received, '%d-%b-%Y') AS Date_Result_Received,
    -- DATE_FORMAT(ll.Effective_Result_Date, '%d-%b-%Y') AS Effective_Result_Date, 
    ll.VL_Result,
    DATE_FORMAT(es.First_EAC, '%d-%b-%Y') AS First_EAC,
    DATE_FORMAT(es.First_EAC_EntryDate, '%d-%b-%Y') AS First_EAC_EntryDate,
    DATE_FORMAT(es.Second_EAC, '%d-%b-%Y') AS Second_EAC,
    DATE_FORMAT(es.Second_EAC_EntryDate, '%d-%b-%Y') AS Second_EAC_EntryDate,
    DATE_FORMAT(es.Third_EAC, '%d-%b-%Y') AS Third_EAC,
    DATE_FORMAT(es.Third_EAC_EntryDate, '%d-%b-%Y') AS Third_EAC_EntryDate,
    DATE_FORMAT(es.Extended_EAC, '%d-%b-%Y') AS Extended_EAC,
    -- es.Extended_EAC_encounter_id AS Extended_EAC_Debug, -- 
    es.Barriers_to_Adherence AS Barriers_to_Adherence,
    DATE_FORMAT(sl.Subsequent_Retest_Date, '%d-%b-%Y') AS Repeat_Sample_Collection_Date,
    sl.subsequent_vl AS Repeat_vl_Count,
    DATE_FORMAT(sl.subsequent_VL_Result_Date, '%d-%b-%Y') AS Repeat_VL_Result_Date,
    DATE_FORMAT(sl.subsequent_Date_Result_Received, '%d-%b-%Y') AS Repeat_Date_Result_Received,

    CASE 

        WHEN (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl <= 49 
        THEN 'EAC Completed and Suppressed'
        
   
        WHEN (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl BETWEEN 50 AND 999 
        THEN 'EAC Completed with Low Viremia Result'
        
  
        WHEN (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl >= 1000 
        THEN 'EAC Completed with Unsuppressed Result'
        
 
        WHEN (es.First_EAC IS NULL AND es.Second_EAC IS NULL AND es.Third_EAC IS NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl <= 49 
        THEN 'Suppressed Result without EAC'
        

        WHEN (es.First_EAC IS NULL OR es.Second_EAC IS NULL OR es.Third_EAC IS NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl <= 49 
        THEN 'Suppressed Result with Incomplete EAC'
        
   
        WHEN (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.Subsequent_Retest_Date IS NOT NULL 
             AND sl.subsequent_VL_Result_Date IS NULL 
        THEN 'EAC Completed Awaiting Result'
        

        WHEN (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.Subsequent_Retest_Date IS NULL 
        THEN 'EAC Completed Awaiting Sample Collection'
        
    
        WHEN (es.First_EAC IS NOT NULL OR es.Second_EAC IS NOT NULL OR es.Third_EAC IS NOT NULL)
             AND NOT (es.First_EAC IS NOT NULL AND es.Second_EAC IS NOT NULL AND es.Third_EAC IS NOT NULL)
             AND sl.subsequent_vl IS NULL
        THEN 'EAC Ongoing'
      
        WHEN (es.First_EAC IS NULL OR es.Second_EAC IS NULL OR es.Third_EAC IS NULL)
             AND sl.subsequent_vl IS NOT NULL 
             AND sl.subsequent_vl > 49 
        THEN 'Incomplete EAC with Unsuppressed Result'
        
      
        WHEN (es.First_EAC IS NULL AND es.Second_EAC IS NULL AND es.Third_EAC IS NULL)
             AND sl.subsequent_vl IS NULL 
        THEN 'No EAC No Result'
        
      
        ELSE 'Status Unknown'
        
    END AS EAC_Status
FROM LatestLab_temp ll
    INNER JOIN PatientDetails pd ON ll.person_id = pd.person_id
    LEFT JOIN EACSessions es ON ll.person_id = es.person_id AND ll.encounter_id = es.lab_encounter_id
    LEFT JOIN SubsequentLab sl ON ll.person_id = sl.person_id AND ll.encounter_id = sl.lab_encounter_id
    CROSS JOIN FacilityDetails fd
ORDER BY ll.person_id, ll.Sample_Collection_Date ASC;


DROP TABLE IF EXISTS ALL_Lab_temp;
DROP TABLE IF EXISTS LatestLab_temp;
DROP TEMPORARY TABLE IF EXISTS EACSessions;
DROP TEMPORARY TABLE IF EXISTS SubsequentLab;
DROP TEMPORARY TABLE IF EXISTS PatientDetails;
DROP TEMPORARY TABLE IF EXISTS FacilityDetails;