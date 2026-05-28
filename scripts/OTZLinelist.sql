-- ---------------------------------------------------------------------------
-- Date range for the patient-program enrolment filter below.
--
-- This script was authored for a templated report engine (BIRT / OpenMRS
-- Reporting Module / similar) that substituted :startDate and :endDate before
-- sending SQL to MySQL. mysql.connector — and MySQL itself — do not recognise
-- :-prefixed placeholders, so we define MySQL session variables here and use
-- @startDate / @endDate in the WHERE clause instead.
--
-- Defaults below = every enrolment on or before today (deliberately wide so a
-- run never silently omits records). To bound the report to a specific period,
-- edit the two values, e.g. @startDate = '2025-10-01' for FY26 Q1.
-- ---------------------------------------------------------------------------
SET @startDate = '1900-01-01';
SET @endDate   = NOW();

SELECT
   global_property.property_value as DatimCode,
   person.person_id,
   pid2.identifier as PepfarID,
   pid3.identifier as HospID,
   person.gender as Sex,
   DATE_FORMAT(person.birthdate,'%d-%b-%Y') as DOB,
   @lastPickup:=getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,curdate())) as LastPickupDate,
   DATE_FORMAT(getdatevalueobsid(getmaxconceptobsid(patient.patient_id,159599,curdate())),'%d-%b-%Y') as ARTStartDate,
    
   DATE_FORMAT(pprg.date_enrolled,'%d-%b-%Y') as DateEnrolledIntoOTZ,
   
   TIMESTAMPDIFF(YEAR,person.birthdate,pprg.date_enrolled) as `AgeAtEnrollment`,
   
   TIMESTAMPDIFF(YEAR,person.birthdate,CURDATE()) as `CurrentAge`,
	 
	 
	 DATE_FORMAT(getobsdatetime(
previousobsfromform(
patient.patient_id,
5096,
27,
pprg.date_enrolled
)
),'%d-%b-%Y') as `AppointmentBeforeEnrollment`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsid(patient.patient_id,162240,pprg.date_enrolled)),'%d-%b-%Y') as `PickupDateBeforeEnrollment`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165290,14,pprg.date_enrolled)) as `AdherenceBeforeEnrollment`,

getnumericvalueobsid(getmaxconceptobsid(patient.patient_id,856,pprg.date_enrolled)) as `ViralLoadBeforeEnrollment`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsid(patient.patient_id,856,pprg.date_enrolled)),'%d-%b-%Y') as `ViralLoadBeforeEnrollmentDate`,
@otzmonths:=TIMESTAMPDIFF(MONTH,pprg.date_enrolled,@lastPickup) as `DurationOnOTZ`,
getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH))) as `ViralLoad6Months`,

IF(@otzmonths>12,getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH))),"") as `ViralLoad12Months`,

IF(@otzmonths>18,getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH))),"") as `ViralLoad18Months`,

IF(@otzmonths>24,getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH))),"") as `ViralLoad24Months`,

DATE_FORMAT(getobsdatetime(
getmaxconceptobsidwithformid(
patient.patient_id,
856,
21,
DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH)

)),'%d-%b-%Y')
 as `ViralLoad6MonthsDate`,
 
 IF(@otzmonths>12,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH))),'%d-%b-%Y'),"") as 
`ViralLoad12MonthsDate`,
 
 IF(@otzmonths>18,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH))),'%d-%b-%Y'),"") as 
`ViralLoad18MonthsDate`,

IF(@otzmonths>24,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,856,21,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH))),'%d-%b-%Y'),"") as 
`ViralLoad24MonthsDate`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH))) as `Adherence6Months`,

IF(@otzmonths>12,getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH))),"") as `Adherence12Months`,

IF(@otzmonths>18,getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH))),"") as `Adherence18Months`,

IF(@otzmonths>24,getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH))),"") as `Adherence24Months`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH))),'%d-%b-%Y') as `Adherence6MonthsDate`,

IF(@otzmonths>12,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH))),'%d-%b-%Y'),"") as `Adherence12MonthsDate`,

IF(@otzmonths>18,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH))),'%d-%b-%Y'),"") as `Adherence18MonthsDate`,

IF(@otzmonths>24,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165290,14,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH))),'%d-%b-%Y'),"") as `Adherence24MonthsDate`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH))),'%d-%b-%Y') as `PickupDate6Months`,

IF(@otzmonths>12,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH))),'%d-%b-%Y'),"") as `PickupDate12Months`,

IF(@otzmonths>18,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH))),'%d-%b-%Y'),"") as `PickupDate18Months`,

IF(@otzmonths>24,DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH))),'%d-%b-%Y'),"") as `PickupDate24Months`,

getconceptval(getmaxconceptobsid(patient.patient_id,162240,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH)),159368,patient.patient_id) as `DaysOfARVRefil6Months`,

IF(@otzmonths>12,getconceptval(getmaxconceptobsid(patient.patient_id,162240,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH)),159368,patient.patient_id),"") as `DaysOfARVRefil12Months`,

IF(@otzmonths>18,getconceptval(getmaxconceptobsid(patient.patient_id,162240,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH)),159368,patient.patient_id),"") as `DaysOfARVRefil18Months`,

IF(@otzmonths>24,getconceptval(getmaxconceptobsid(patient.patient_id,162240,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH)),159368,patient.patient_id),"") as `DaysOfARVRefil24Months`,

DATE_FORMAT(getobsdatetime(
previousobsfromform(
patient.patient_id,
5096,
27,
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 6 MONTH)))
)
),'%d-%b-%Y') as `AppointmentDate6Months`,

IF(@otzmonths>12,DATE_FORMAT(getobsdatetime(
previousobsfromform(
patient.patient_id,
5096,
27,
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 12 MONTH)))
)
),'%d-%b-%Y'),"") as `AppointmentDate12Months`,

IF(@otzmonths>18,DATE_FORMAT(getobsdatetime(
previousobsfromform(
patient.patient_id,
5096,
27,
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 18 MONTH)))
)
),'%d-%b-%Y'),"") as `AppointmentDate18Months`,

IF(@otzmonths>24,DATE_FORMAT(getobsdatetime(
previousobsfromform(
patient.patient_id,
5096,
27,
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,DATE_ADD(pprg.date_enrolled, INTERVAL 24 MONTH)))
)
),'%d-%b-%Y'),"") as `AppointmentDate24Months`,

getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,164507,curdate())) `FirstPedFirstLineDate`,
   
   getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,164514,curdate())) `FirstPedSecondLineDate`,
   
   getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,165703,curdate())) `FirstPedThirdLineDate`,
   
   
   getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,164506,curdate())) `FirstAdultFirstLineDate`,
   
   getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,164513,curdate())) `FirstAdultSecondLineDate`,
   
   getobsdatetime(getminconceptobswithformidvaluecoded(patient.patient_id,165708,27,165702,curdate())) `FirstAdultThirdLineDate`,
   getcodedvalueobsid(getminconceptobswithformid(patient.patient_id,165470,13,curdate())) as `PatientOutcome`,
   getobsdatetime(getminconceptobswithformid(patient.patient_id,165470,13,curdate())) as `PatientOutcomeDate`,
   
   IFNULL (getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,curdate())),getoutcome(
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,curdate())),
getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,curdate()),159368,patient.patient_id) ,
28,
curdate()

))  as `CurrentARTStatus`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166273,73,curdate())),'%d-%b-%Y') as   
`DateTransitionedToAdultClinic`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166275,73,curdate())) as `OTZProgramOutcome`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166008,73,curdate())),'%d-%b-%Y') as   
`DateofOutcome`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166355,73,curdate())) as `ReturningPatient`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166354,73,curdate())),'%d-%b-%Y') as `DateReturned`,
   
gettextvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166352,73,curdate())) as `ReactivatedBy`,

completedayp(patient.patient_id) as `CompletedAYPModule`,
   IF(completedayp(patient.patient_id)='Yes',DATE_FORMAT(datecompletedayp(patient.patient_id),'%d-%b-%Y'),NULL) as `DateCompletedAYPModule`






 
	 
	 FROM
   patient
   INNER JOIN (select DISTINCT encounter.patient_id from encounter where encounter.form_id=73 and encounter.voided=0) as innerquery on(innerquery.patient_id=patient.patient_id and patient.voided=0)
   
   LEFT JOIN person ON(person.person_id=patient.patient_id and person.voided=0)
  
   LEFT JOIN patient_identifier pid2 on(pid2.patient_id=patient.patient_id and patient.voided=0 and pid2.identifier_type=4 and pid2.voided=0)
   LEFT JOIN patient_identifier pid3 on(pid3.patient_id=patient.patient_id and patient.voided=0 and pid3.identifier_type=5 and pid3.voided=0)
   LEFT JOIN global_property on(global_property.property='facility_datim_code')
   LEFT JOIN patient_program pprg on(pprg.patient_id=patient.patient_id and pprg.program_id=5 and pprg.voided=0)
   WHERE patient.voided=0 
  and
   pprg.date_enrolled BETWEEN @startDate and @endDate

   GROUP BY patient.patient_id;
   

   