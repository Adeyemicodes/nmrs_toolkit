/* 
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 * ╔═══════════════════════════════════════════════════════════════════════════════════════════════════════════╗
 * ║                          				 NMRS Schema Functions                                             ║
 * ╚═══════════════════════════════════════════════════════════════════════════════════════════════════════════╝
 * ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
 * 
 * Description:  MySQL script that resolves issues with scripts that depends on missing functions
 *               This creates all essential functions that are often skipped when dumping a schema
 * 
 * Version:      2.0
 * Created:      September 27, 2024
 * Updated:	 	 August 3, 2025
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

-- Drop functions if they exist before creating them
DROP FUNCTION IF EXISTS `completedayp`;
DROP FUNCTION IF EXISTS `datecompletedayp`;
DROP FUNCTION IF EXISTS `get_concept_name`;
DROP FUNCTION IF EXISTS `getcodedintvalueobs`;
DROP FUNCTION IF EXISTS `getcodedvalueobsid`;
DROP FUNCTION IF EXISTS `getconceptval`;
DROP FUNCTION IF EXISTS `getconceptvalobsid`;
DROP FUNCTION IF EXISTS `getconceptvalV2`;
DROP FUNCTION IF EXISTS `getcurrentregimen`;
DROP FUNCTION IF EXISTS `getdatevalueobsid`;
DROP FUNCTION IF EXISTS `getdaysofarvrefil`;
DROP FUNCTION IF EXISTS `getencounterdate`;
DROP FUNCTION IF EXISTS `getencounterid`;
DROP FUNCTION IF EXISTS `getendofquarter`;
DROP FUNCTION IF EXISTS `getlastencounter`;
DROP FUNCTION IF EXISTS `getlastvisitdate`;
DROP FUNCTION IF EXISTS `getmaxconceptobsid`;
DROP FUNCTION IF EXISTS `getmaxconceptobsidwithformid`;
DROP FUNCTION IF EXISTS `getmaxconceptobsidwithformidvaluecoded`;
DROP FUNCTION IF EXISTS `getminconceptobsid`;
DROP FUNCTION IF EXISTS `getminconceptobsidwithformid`;
DROP FUNCTION IF EXISTS `getminconceptobswithformid`;
DROP FUNCTION IF EXISTS `getminconceptobswithformidvaluecoded`;
DROP FUNCTION IF EXISTS `getnumericvalueobsid`;
DROP FUNCTION IF EXISTS `getobsdatetime`;
DROP FUNCTION IF EXISTS `getobswithencounterid`;
DROP FUNCTION IF EXISTS `getoutcome`;
DROP FUNCTION IF EXISTS `getoutcome2`;
DROP FUNCTION IF EXISTS `getoutcomewithpillbalanceandfrequency`;
DROP FUNCTION IF EXISTS `getreporteddate`;
DROP FUNCTION IF EXISTS `gettextvalueobsid`;
DROP FUNCTION IF EXISTS `modulescompleted`;
DROP FUNCTION IF EXISTS `previous`;
DROP FUNCTION IF EXISTS `previousobsfromform`;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `completedayp`(patientid int) RETURNS varchar(10) CHARSET utf8
BEGIN

DECLARE total INT;

DECLARE ans VARCHAR(10);

select count(distinct concept_id) into total from obs where person_id=patientid and concept_id in(166261,166262,166263,166264,166265,166266,166268) and voided=0;

IF total < 7
  THEN SET ans = 'No';
ELSE  
  SET ans = 'Yes';
END IF;
RETURN ans;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `datecompletedayp`(patientid int) RETURNS date
BEGIN

DECLARE completion_date DATE;

select max(obs.value_datetime) into completion_date from obs where person_id=patientid and concept_id in(166261,166262,166263,166264,166265,166266,166268) and voided=0;

RETURN completion_date;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `get_concept_name`(conceptid INT) RETURNS text CHARSET latin1
    READS SQL DATA
    DETERMINISTIC
BEGIN
	RETURN (SELECT NAME FROM  concept_name  WHERE concept_id = conceptid AND locale = 'en' AND locale_preferred = 1 LIMIT 1);
	
	
    END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getcodedintvalueobs`(`obsid` int) RETURNS int(11)
BEGIN 

    DECLARE val INT;

    SELECT  obs.value_coded INTO val from 
		obs 
		where obs.obs_id=obsid;
		
	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getcodedvalueobsid`(`obsid` int) RETURNS text CHARSET utf8
BEGIN

    DECLARE val TEXT;

    SELECT  cn.name INTO val from 
		obs 
		inner join concept_name cn on(obs.value_coded=cn.concept_id and cn.locale='en' and cn.locale_preferred=1) where obs.obs_id=obsid;
		

	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getconceptval`(`obsid` int,`cid` int, pid int) RETURNS decimal(10,0)
BEGIN
	#Routine body goes here...
   DECLARE value_num INT;
    SELECT obs.value_numeric into value_num from obs WHERE  obs.obs_group_id is not null and obs.obs_group_id=obsid and obs.concept_id=cid and obs.person_id=pid and obs.voided=0 limit 1;
	RETURN value_num;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`openmrs_user`@`localhost` FUNCTION `getconceptvalobsid`(`obsid` INT,`cid` INT, pid INT) RETURNS decimal(10,0)
BEGIN
    DECLARE value_num INT;
    SELECT DISTINCT obs.obs_id INTO value_num FROM obs
        WHERE  obs.obs_group_id IS NOT NULL
        AND obs.obs_group_id=obsid
        AND obs.concept_id=cid
        AND obs.person_id=pid
        AND obs.voided=0
        ORDER BY obs_id ASC
        LIMIT 1;
	RETURN value_num;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getconceptvalV2`(`encounter_id` INT,`cid` INT, pid INT) RETURNS decimal(10,0)
BEGIN
    DECLARE value_num INT;
       SELECT obs1.value_numeric INTO value_num FROM obs obs1
	INNER JOIN obs obs2 ON(obs1.`obs_group_id` = obs2.obs_id AND obs2.concept_id = 162240 AND obs1.encounter_id = encounter_id)
	WHERE obs1.value_numeric IS NOT NULL  AND obs1.concept_id = cid AND obs1.person_id =pid
	ORDER BY obs1.obs_datetime DESC LIMIT 1;
	RETURN value_num;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getcurrentregimen`(`cid` INT,`eid` INT) RETURNS text CHARSET utf8
BEGIN
 DECLARE regimen TEXT;
   SELECT cn1.name INTO regimen
   FROM obs
   INNER JOIN concept_name cn1
   ON (obs.value_coded=cn1.concept_id
        AND cn1.locale='en'
        AND cn1.locale_preferred=1)
    WHERE obs.concept_id=cid
    AND obs.encounter_id=eid
    AND obs.voided=0 LIMIT 1;

	RETURN regimen;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getdatevalueobsid`(`obsid` int) RETURNS date
BEGIN

    DECLARE val DATE;

    SELECT  obs.value_datetime into val from obs WHERE  obs.obs_id=obsid;

	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getdaysofarvrefil`(`obsid` numeric,`obsgroupid` numeric,`valuenumeric` numeric) RETURNS decimal(10,0)
BEGIN
	#Routine body goes here...
DECLARE ans NUMERIC;

IF obsid=obsgroupid THEN
        SET ans = valuenumeric;
ELSE
         SET ans = null;
END IF;

RETURN ans;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getencounterdate`(`eid` LONG) RETURNS datetime
BEGIN
	
	DECLARE enc_date DATETIME;
	
	select encounter.encounter_datetime into enc_date from encounter where encounter.encounter_id=eid;

	RETURN enc_date;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getencounterid`(`obsid` int) RETURNS int(11)
BEGIN

    DECLARE val INT;

    SELECT  obs.encounter_id INTO val from 
		obs 
		where obs.obs_id=obsid;
		

	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getendofquarter`(`date_val` date) RETURNS date
BEGIN
	DECLARE fyear INT;
	DECLARE fquarter INT;
	DECLARE start_date DATE;
	DECLARE end_date DATE;
	
	DECLARE month_val INT;
	
	
	
	SET fyear=IF(QUARTER(date_val)=4,YEAR(date_val)+1,YEAR(date_val));
	SET fquarter=IF(QUARTER(date_val)=4,MOD(QUARTER(date_val)+1,4),QUARTER(date_val)+1);
	
	SELECT CASE  
	WHEN fquarter=1 THEN 12
	WHEN fquarter=2 THEN 3
	WHEN fquarter=3 THEN 6
	WHEN fquarter=4 THEN 9
	END INTO month_val;
	
	
	
	
	SELECT STR_TO_DATE(CONCAT(fyear,"-",month_val,"-",1),'%Y-%c-%e') INTO start_date;
	
	SELECT LAST_DAY(start_date) INTO end_date;
	

	RETURN end_date;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getlastencounter`(`patient_id` int,`form_id` int,`cuttoffdate` date) RETURNS mediumtext CHARSET utf8
BEGIN

	 DECLARE enc_id LONG;
	 
	 select encounter_id into enc_id from encounter where encounter.form_id=form_id and encounter.patient_id=patient_id and encounter.voided=0 AND encounter.encounter_datetime<=cuttoffdate order by encounter_datetime desc limit 1;
	 
	RETURN enc_id;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getlastvisitdate`(`patient_id` int,`cuttoffdate` DATE) RETURNS int(11)
BEGIN
	DECLARE encid INT;
	select encounter.encounter_id into encid from encounter where encounter.voided=0 and form_id in(22,56,14,69,23,44,74,53,21,73,20,27,67) and encounter.encounter_datetime<=cuttoffdate and encounter.patient_id=patient_id order by encounter.encounter_datetime DESC LIMIT 1;

	RETURN encid;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getmaxconceptobsid`(`patientid` int,`conceptid` int, `cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getmaxconceptobsidwithformid`(`patientid` int,`conceptid` int, `formid` int,`cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  encounter.form_id=formid and obs.person_id=patientid 
		and obs.concept_id=conceptid and obs.voided=0 and obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getmaxconceptobsidwithformidvaluecoded`(`patientid` int,`conceptid` int, `formid` int,val int,`cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  encounter.form_id=formid and obs.person_id=patientid 
		and obs.concept_id=conceptid and obs.voided=0 and obs.value_coded=val and obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getminconceptobsid`(`patientid` int,`conceptid` int, `cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<=cutoffdate ORDER BY obs.obs_datetime ASC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`openmrs_user`@`localhost` FUNCTION `getminconceptobsidwithformid`(`patientid` int,`conceptid` int, `formid` int) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  encounter.form_id=formid and obs.person_id=patientid 
		and obs.concept_id=conceptid and obs.voided=0 ORDER BY obs.obs_datetime ASC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getminconceptobswithformid`(`patientid` int,`conceptid` int, `formid` int, `cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<=cutoffdate and encounter.form_id=formid  ORDER BY obs.obs_datetime ASC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getminconceptobswithformidvaluecoded`(`patientid` int,`conceptid` int, `formid` int, `val_coded` int, `cutoffdate` DATE) RETURNS decimal(10,0)
BEGIN

    DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<=cutoffdate and encounter.form_id=formid and obs.value_coded=val_coded  ORDER BY obs.obs_datetime ASC LIMIT 1;

	RETURN value_num;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getnumericvalueobsid`(`obsid` int) RETURNS decimal(10,2)
BEGIN

    DECLARE val DECIMAL(10,2);

    SELECT  obs.value_numeric INTO val from obs WHERE  obs.obs_id=obsid;
	RETURN val;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getobsdatetime`(`obsid` int) RETURNS date
BEGIN

    DECLARE val DATE;

    SELECT  obs.obs_datetime INTO val from obs WHERE  obs.obs_id=obsid;

	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getobswithencounterid`(`encounter_id` int,`concept_id` int) RETURNS int(11)
BEGIN
	DECLARE obsid INT;
	
        SELECT obs.obs_id into obsid from obs where obs.voided=0 and obs.concept_id=concept_id and obs.encounter_id=encounter_id LIMIT 1;

	RETURN obsid;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getoutcome`(`Pharmacy_LastPickupdate` date,`daysofarvrefill` numeric,`LTFUdays` numeric, `today_date` date) RETURNS text CHARSET utf8
BEGIN

DECLARE  LTFUdate DATE;

DECLARE  LTFUnumber NUMERIC;
DECLARE  daysdiff NUMERIC;
DECLARE outcome text;

SET LTFUnumber=daysofarvrefill+LTFUdays;   -- eg 60 days refills plus 28 days ---
SELECT DATE_ADD(Pharmacy_LastPickupdate, INTERVAL LTFUnumber DAY) INTO LTFUdate;  -- LastPickup + 60 days +28 days --- = LTFUdate
SELECT DATEDIFF(LTFUdate,today_date) into daysdiff;  -- LTFUdate - today's date = daysdiff
SELECT IF(daysdiff >=0,"Active","LTFU") into outcome;

RETURN outcome;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`openmrs_user`@`localhost` FUNCTION `getoutcome2`(lastpickupdate DATE,daysofarvrefill NUMERIC,ltfudays NUMERIC, enddate DATE) RETURNS text CHARSET utf8
BEGIN DECLARE  ltfudate DATE; DECLARE  ltfunumber NUMERIC; DECLARE  daysdiff NUMERIC; DECLARE  outcome TEXT; SET ltfunumber=daysofarvrefill+ltfudays; SELECT DATE_ADD(lastpickupdate, INTERVAL ltfunumber DAY) INTO ltfudate; SELECT DATEDIFF(ltfudate,enddate) INTO daysdiff; SELECT IF(lastpickupdate IS NULL,"",IF(daysdiff >=0,"Active","InActive")) INTO outcome; RETURN outcome; END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`openmrs_user`@`localhost` FUNCTION `getoutcomewithpillbalanceandfrequency`(`lastpickupdate` DATE,`daysofarvrefill` NUMERIC, `ltfudays` NUMERIC, `pillbalance` NUMERIC, `obsid` int, `enddate` DATE) RETURNS text CHARSET utf8
BEGIN
        DECLARE  ltfudate DATE;
        DECLARE  ltfunumber NUMERIC;
        DECLARE  daysdiff NUMERIC;
        DECLARE outcome TEXT;
        DECLARE frequencycode NUMERIC;
        DECLARE evaluatedpillbalance INT;
	
		SELECT obs.value_coded INTO frequencycode FROM obs WHERE obs.obs_id=obsid;
        
    	SELECT CASE  
		WHEN frequencycode=160870 THEN FLOOR(pillbalance/4)
		WHEN frequencycode=166057 THEN FLOOR(pillbalance/8)
		WHEN frequencycode=166056 THEN FLOOR(pillbalance/6)
		WHEN frequencycode=165721 THEN FLOOR(pillbalance/4)
        WHEN frequencycode=160858 THEN FLOOR(pillbalance/2)
        ELSE pillbalance
		END INTO evaluatedpillbalance;
		
        SET ltfunumber=daysofarvrefill+ltfudays+evaluatedpillbalance;
        SELECT DATE_ADD(lastpickupdate, INTERVAL ltfunumber DAY) INTO ltfudate;
        SELECT DATEDIFF(ltfudate,enddate) INTO daysdiff;
        SELECT IF(lastpickupdate IS NULL,"",IF(daysdiff >=0,"Active","InActive")) INTO outcome;
        RETURN outcome;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `getreporteddate`(`encounter_id` INT,`reporteddateconcept_id` INT,`patientid` INT) RETURNS date
BEGIN
        DECLARE val_date DATE;
        SELECT obs.value_datetime INTO val_date FROM obs
        WHERE concept_id=reporteddateconcept_id
        AND obs.encounter_id=encounter_id
        AND obs.person_id=patientid
        AND voided=0 LIMIT 1;
        RETURN val_date;
    END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `gettextvalueobsid`(`obsid` int) RETURNS text CHARSET utf8
BEGIN

    DECLARE val TEXT;

    SELECT  obs.value_text INTO val from obs WHERE  obs.obs_id=obsid;

	RETURN val;

END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `modulescompleted`(patientid INT, module_concept_id INT) RETURNS varchar(10) CHARSET utf8
BEGIN
    DECLARE total INT;
    DECLARE ans VARCHAR(10);

    SELECT COUNT(*) INTO total 
    FROM obs 
    WHERE person_id = patientid AND concept_id = module_concept_id AND voided = 0;

    IF total > 0 THEN
        SET ans = 'Yes';
    ELSE
        SET ans = 'No';
    END IF;

    RETURN ans;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `previous`(patientid int, conceptid int, cuttoffdate DATE) RETURNS int(11)
BEGIN
DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs WHERE  obs.person_id=patientid and obs.concept_id=conceptid and obs.voided=0 and 
	obs.obs_datetime<cuttoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;

	RETURN value_num;
END$$
DELIMITER ;

DELIMITER $$
CREATE DEFINER=`root`@`localhost` FUNCTION `previousobsfromform`(patientid int, conceptid int, formid int, cuttoffdate DATE) RETURNS int(11)
BEGIN
DECLARE value_num INT;

    SELECT  obs.obs_id into value_num from obs inner join encounter on(encounter.encounter_id=obs.encounter_id and encounter.voided=0) WHERE
    obs.person_id=patientid and obs.concept_id=conceptid and encounter.form_id=formid and obs.voided=0 and 
	obs.obs_datetime<cuttoffdate ORDER BY obs.obs_datetime DESC LIMIT 1;

	RETURN value_num;
END$$
DELIMITER ;