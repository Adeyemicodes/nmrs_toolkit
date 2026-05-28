-- ARTLinelist Report  FROM nmrsreports-1.0.6.5

SET @endDate = now();


select 
nigeria_datimcode_mapping.state_name as `State`,
nigeria_datimcode_mapping.lga_name as `LGA`,
gp1.property_value as DatimCode,
gp2.property_value as FacilityName,
pid1.identifier as `PatientUniqueID`,
pid2.identifier as  `PatientHospitalNo`,
pid3.identifier as  `ANCNoIdentifier`,
gettextvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165567,16,@endDate)) as `ANCNoConceptID`,
pid4.identifier as  `HTSNo`,
person.gender as `Sex`,
MAX(IF(obs.concept_id=159599,IF(TIMESTAMPDIFF(YEAR,person.birthdate,obs.value_datetime)>=5,TIMESTAMPDIFF(YEAR,person.birthdate,obs.value_datetime),@ageAtStart:=0),null)) as  `AgeAtStartOfARTYears`,
MAX(IF(obs.concept_id=159599,IF(TIMESTAMPDIFF(YEAR,person.birthdate,obs.value_datetime)<5,TIMESTAMPDIFF(MONTH,person.birthdate,obs.value_datetime),null),null)) as `AgeAtStartOfARTMonths`,

MAX(IF(obs.concept_id=160540,cn1.name,null)) as CareEntryPoint,

MAX(IF(obs.concept_id=160554,obs.value_datetime, NULL)) as  `HIVConfirmedDate`,

TIMESTAMPDIFF(MONTH,getdatevalueobsid(getmaxconceptobsid(patient.patient_id,159599,@endDate)),getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate))) as MonthsOnART,

MAX(IF(obs.concept_id=160534,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'),null)) as DateTransferredIn,

MAX(IF(obs.concept_id=165242,cn1.name,null)) as TransferInStatus,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsid(patient.patient_id,159599,@endDate)),'%d-%b-%Y') as `ARTStartDate`,


DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),'%d-%b-%Y') as `LastPickupDate`,

DATE_FORMAT(getencounterdate(getlastvisitdate(patient.patient_id,@endDate)),'%d-%b-%Y') as LastVisitDate,

																																  

getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate),159368,patient.patient_id) as `DaysOfARVRefil`,

getobsvaluetextwithencounterid(getlastencounter(patient.patient_id,27,@endDate))
as `PillBalance`,

MAX(IF(obs2.concept_id=165708,cn2.name,null)) as `InitialRegimenLine`,

MAX(IF(obs2.concept_id=165708,getcurrentregimen(obs2.value_coded,obs2.encounter_id),null)) as `InitialRegimen`,

getnumericvalueobsid(getminconceptobswithformid(patient.patient_id,5497,21,@endDate)) as `InitialCD4Count`,

DATE_FORMAT(getobsdatetime(getminconceptobswithformid(patient.patient_id,5497,21,@endDate)),'%d-%b-%Y') as `InitialCD4CountDate`,

getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,5497,21,@endDate)) as `CurrentCD4Count`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,5497,21,@endDate)),'%d-%b-%Y') as `CurrentCD4CountDate`,

DATE_FORMAT(getencounterdate(getlastencounter(patient.patient_id,69,@endDate)),'%d-%b-%Y') 
as `LastEACDate`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165708,27,@endDate)) as `CurrentRegimenLine`,

getcurrentregimen(getcodedintvalueobs(getmaxconceptobsidwithformid(patient.patient_id,165708,27,@endDate)),getencounterid(getmaxconceptobsidwithformid(patient.patient_id,165708,27,@endDate))) as `CurrentRegimen`,

IF(person.gender='F',getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),null) as `PregnancyStatus`,

IF(person.gender='F',DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),'%d-%b-%Y'),null) as `PregnancyStatusDate`,


IF(person.gender='F', DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),5596)),'%d-%b-%Y'), null) as `EDD`,

IF(person.gender='F',DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),'%d-%b-%Y'),null) as `LastDeliveryDate`,

IF(person.gender='F', DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),1427)),'%d-%b-%Y'), null) as `LMP`,

IF(person.gender='F', getnumericvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,165050,14,@endDate)),1438)), null) as `GestationAgeWeeks`,

getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)) as `CurrentViralLoad(c/ml)`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),'%d-%b-%Y') as `ViralLoadEncounterDate`,

DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),159951)),'%d-%b-%Y') as `ViralLoadSampleCollectionDate`,

DATE_FORMAT(getreporteddate(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),165414,patient.patient_id),'%d-%b-%Y') 
as `ViralLoadReportedDate`,

DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),166423)),'%d-%b-%Y') as `ResultDate`,

DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),166424)),'%d-%b-%Y') as `AssayDate`,

DATE_FORMAT(getdatevalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),166425)),'%d-%b-%Y') as `ApprovalDate`,

getcodedvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,856,21,@endDate)),164980)) as `ViralLoadIndication`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,@endDate)) as PatientOutcome,
DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165470,13,@endDate)),'%d-%b-%Y') as PatientOutcomeDate,

IFNULL (getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,@endDate)),getoutcome(
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),
getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate),159368,patient.patient_id) ,
28,
IF(@endDate IS NULL or @endDate = '', CURDATE(),@endDate)

))  as `CurrentARTStatus`,

getcodedvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),166148)) as `DispensingModality`,

getcodedvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),166276)) as `FacilityDispensingModality`,

getcodedvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),166363)) as `DDDDispensingModality`,

getcodedvalueobsid(getobswithencounterid(getencounterid(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),166278)) as `MMDType`,


MAX(IF(obs.concept_id=165775,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'),null)) as `DateReturnedToCare`,

MAX(IF(obs.concept_id=165469,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'),null)) as `DateOfTermination`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,5096,27,@endDate)),'%d-%b-%Y') as `PharmacyNextAppointment`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,5096,14,@endDate)),'%d-%b-%Y') as `ClinicalNextAppointment`,

IF(TIMESTAMPDIFF(YEAR,person.birthdate,curdate())>=5,TIMESTAMPDIFF(YEAR,person.birthdate,curdate()),null) as `CurrentAgeYears`,
IF(TIMESTAMPDIFF(YEAR,person.birthdate,curdate())<5,TIMESTAMPDIFF(MONTH,person.birthdate,curdate()),null) as `CurrentAgeMonths`,
DATE_FORMAT(person.birthdate,'%d-%b-%Y') as `DateOfBirth`,
IF(person.dead=1,"Dead","") as MarkAsDeseased,
IF(person.dead=1,person.death_date,"") as MarkAsDeseasedDeathDate,
CONCAT("234-",TRIM(LEADING '0' FROM psn_atr.value)) AS RegistrationPhoneNo,
MAX(IF(obs.concept_id=159635,CONCAT("+234-",TRIM(LEADING '0' FROM obs.value_text)),null)) as `NextofKinPhoneNo`,
MAX(IF(obs.concept_id=160642,CONCAT("+234-",TRIM(LEADING '0' FROM obs.value_text)),null)) as 
`TreatmentSupporterPhoneNo`,

IF(biometrictable.patient_Id IS NOT NULL,'Yes','No') as BiometricCaptured,

IF(biometrictable.patient_Id IS NOT NULL,DATE_FORMAT(biometrictable.date_created,'%d-%b-%Y'),"") as BiometricCaptureDate,

IF(biometrictable.patient_Id IS NOT NULL,IF(invalidprint.patient_Id IS NOT NULL,'No','Yes'),"") as ValidCapture,

getnumericvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,5089,14,@endDate)) as `CurrentWeight(Kg)`,
DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,5089,14,@endDate)),'%d-%b-%Y') as `CurrentWeightDate`,
getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,1659,14,@endDate)) as `TBStatus`,
DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,1659,14,@endDate)),'%d-%b-%Y') as `TBStatusDate`,
DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,164852,56,@endDate)),'%d-%b-%Y')
as `BaselineINHStartDate`,
DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166096,56,@endDate)),'%d-%b-%Y')
as `BaselineINHStopDate`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165994,53,@endDate)),'%d-%b-%Y')
as `CurrentINHStartDate`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166007,53,@endDate)) as `CurrentINHOutcome`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166008,53,@endDate)),'%d-%b-%Y')
as `CurrentINHOutcomeDate`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformidvaluecoded(patient.patient_id,165727,27,1679,@endDate)),'%d-%b-%Y') 
as `LastINHDispensedDate`,
DATE_FORMAT(getdatevalueobsid(getmaxconceptobsid(patient.patient_id,1113,@endDate)),'%d-%b-%Y') 
as `BaselineTBTreatmentStartDate`,
DATE_FORMAT(getdatevalueobsid(getmaxconceptobsid(patient.patient_id,159431,@endDate)),'%d-%b-%Y')
 as `BaselineTBTreatmentStopDate`,
 DATE_FORMAT(getencounterdate(getlastencounter(patient.patient_id,67,@endDate)),'%d-%b-%Y') 
as `LastViralLoadSampleCollectionFormDate`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,159951,21,@endDate)),'%d-%b-%Y') as `LastSampleTakenDate`, 


DATE_FORMAT(getencounterdate(getlastencounter(patient.patient_id,73,@endDate)),'%d-%b-%Y') 
as `OTZEnrollmentDate`,

DATE_FORMAT(getdatevalueobsid(getmaxconceptobsidwithformid(patient.patient_id,166008,73,@endDate)),'%d-%b-%Y') 
as `OTZOutcomeDate`,
DATE_FORMAT(pprg.date_enrolled,'%d-%b-%Y') as EnrollmentDate,
MAX(IF(obs2.concept_id in(164506,164507), cn2.name,null)) as `InitialFirstLineRegimen`,
MAX(IF(obs2.concept_id in(164506,164507), DATE_FORMAT(obs2.obs_datetime,'%d-%b-%Y'),null)) as `InitialFirstLineRegimenDate`,
MAX(IF(obs2.concept_id in(164513,164514), cn2.name,null)) as `InitialSecondLineRegimen`,
MAX(IF(obs2.concept_id in(164513,164514), DATE_FORMAT(obs2.obs_datetime,'%d-%b-%Y'),null)) as `InitialSecondLineRegimenDate`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH)))),'%d-%b-%Y') as 
`LastPickupDatePreviousQuarter`,

getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH))),159368,patient.patient_id) as 
`DrugDurationPreviousQuarter`,

getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH)))) as 
`PatientOutcomePreviousQuarter`,

DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,165470,13,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH)))),'%d-%b-%Y') as 
`PatientOutcomeDatePreviousQuarter`,
IFNULL(
getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,DATE_SUB(@endDate,INTERVAL 3 MONTH))),
getoutcome(
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH)))),
getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH))),159368,patient.patient_id) ,
28,
IF(@endDate IS NULL or @endDate = '', CURDATE(),getendofquarter(DATE_SUB(@endDate,INTERVAL 3 MONTH)))
)
)  as `ARTStatusPreviousQuarter`,

getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate),160856,patient.patient_id) as `QuantityOfARVDispensedLastVisit`,

getcodedvalueobsid(getconceptvalobsid(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate),165723,patient.patient_id)) as `FrequencyOfARVDispensedLastVisit`,

IFNULL (getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,165470,13,@endDate)),getoutcomewithpillbalance(
getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate)),
getconceptval(getmaxconceptobsidwithformid(patient.patient_id,162240,27,@endDate),159368,patient.patient_id) ,
28,
getlastencounter(patient.patient_id,27,@endDate),

IF(@endDate IS NULL or @endDate = '', CURDATE(),@endDate)

))  as `CurrentARTStatusWithPillBalance`,
DATE_FORMAT(bvinfo.RecaptureDate,'%d-%b-%Y') as RecaptureDate,
bvinfo.recapture_count as RecaptureCount,
getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,167139,14,@endDate)) as `CervicalCancerScreeningStatus`,
DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,167139,14,@endDate)),'%d-%b-%Y') as `CervicalCancerScreeningStatusDate`,
getcodedvalueobsid(getmaxconceptobsidwithformid(patient.patient_id,167150,14,@endDate)) as `CervicalCancerTreatmentProvided`,
DATE_FORMAT(getobsdatetime(getmaxconceptobsidwithformid(patient.patient_id,167150,14,@endDate)),'%d-%b-%Y') as `CervicalCancerTreatmentProvidedDate`





  from patient
  INNER JOIN person on(person.person_id=patient.patient_id and patient.voided=0)
  LEFT JOIN patient_identifier pid1 on(pid1.patient_id=patient.patient_id and patient.voided=0 and pid1.identifier_type=4 and pid1.voided=0)
  
  LEFT JOIN patient_identifier pid3 on(pid3.patient_id=patient.patient_id and patient.voided=0 and pid3.identifier_type=6 and pid3.voided=0)
  
  LEFT JOIN patient_identifier pid4 on(pid4.patient_id=patient.patient_id and patient.voided=0 and pid4.identifier_type=8 and pid4.voided=0)
  
  LEFT JOIN person_attribute psn_atr ON (person.person_id=psn_atr.person_id and psn_atr.person_attribute_type_id=8 and psn_atr.voided=0)
  LEFT JOIN patient_program pprg on(pprg.patient_id=patient.patient_id and pprg.voided=0 and patient.voided=0 and pprg.program_id=1)
  LEFT JOIN patient_identifier pid2 on(pid2.patient_id=patient.patient_id and patient.voided=0 and pid2.identifier_type=5 and pid2.voided=0)
  LEFT JOIN
  (select 
obs.person_id,
obs.concept_id,
 MAX(obs.obs_datetime) as last_date, 
MIN(obs.obs_datetime) as first_date
from obs INNER JOIN encounter on(encounter.encounter_id=obs.encounter_id) where encounter.form_id!=48 and obs.voided=0 and obs.obs_datetime<=@endDate and concept_id in(159599,165708,159368,164506,164513,164507,164514,165702,165703,165050,
856,164980,165470,159635,5089,165988,1659,164852,166096,1113,159431,162240,165242,165724,166156,166158,165727,164982,165414,5596,165775,160540,165469,5096,1427,160642,160534,160554) GROUP BY obs.person_id, obs.concept_id) as sinner on (sinner.person_id=patient.patient_id and patient.voided=0)
INNER JOIN obs on(obs.person_id=patient.patient_id and obs.concept_id=sinner.concept_id and obs.obs_datetime=sinner.last_date and obs.voided=0 and obs.obs_datetime<=@endDate)
INNER JOIN obs obs2 on(obs2.person_id=patient.patient_id and obs2.concept_id=sinner.concept_id and obs2.obs_datetime=sinner.first_date and obs2.voided=0 and obs2.obs_datetime<=@endDate)
LEFT join encounter enc on(enc.encounter_id=obs.encounter_id and enc.voided=0 and obs.voided=0)
left join concept_name cn1 on(obs.value_coded=cn1.concept_id and cn1.locale='en' and cn1.locale_preferred=1)
left join concept_name cn2 on(obs2.value_coded=cn2.concept_id and cn2.locale='en' and cn2.locale_preferred=1)
LEFT JOIN (
   select 
   DISTINCT biometricinfo.patient_Id,biometricinfo.date_created
   from 
   biometricinfo GROUP BY biometricinfo.patient_Id
) as biometrictable 
on(patient.patient_id=biometrictable.patient_Id and patient.voided=0)
LEFT JOIN (
   select 
   DISTINCT biometricinfo.patient_Id
   from 
   biometricinfo where template not like 'Rk1S%' or CONVERT(new_template USING utf8) NOT LIKE 'Rk1S%'
) as invalidprint 
on(patient.patient_id=invalidprint.patient_Id and patient.voided=0)
LEFT JOIN global_property gp1 on(gp1.property='facility_datim_code')
LEFT JOIN global_property gp2 on(gp2.property='Facility_Name')
LEFT JOIN nigeria_datimcode_mapping on(gp1.property_value=nigeria_datimcode_mapping.datim_code)
LEFT JOIN (
select 
encounter.patient_id,
MAX(encounter.encounter_datetime) as last_visit_date
from encounter
where encounter.voided=0 and encounter.form_id!=13 and encounter.encounter_datetime<=@endDate GROUP BY  encounter.patient_id
) as enc_last on(enc_last.patient_id=patient.patient_id and patient.voided=0)
LEFT JOIN (SELECT patient_Id, MAX(date_created) AS RecaptureDate, recapture_count, count(fingerPosition) as NumberOfFingers, MIN(imageQuality) as LowestFPQuality, CEILING(AVG(imageQuality)) as AverageFPQuality, COUNT(IF(imageQuality<80, 1, NULL)) as FPLowQuality, COUNT(IF(imageQuality<80, NULL, 1)) as FPHighQuality
FROM biometricverificationinfo 
GROUP BY patient_Id) as bvinfo on(patient.patient_id=bvinfo.patient_Id)
WHERE patient.voided=0 and pid1.identifier IS NOT NULL
GROUP BY patient.patient_id ;
