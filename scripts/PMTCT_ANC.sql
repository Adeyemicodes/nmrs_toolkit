-- ---------------------------------------------------------------------------
-- Date range for the encounter filter below.
--
-- This script was authored for a templated report engine (BIRT / OpenMRS
-- Reporting Module / similar) that substituted :startDate and :endDate before
-- sending SQL to MySQL. mysql.connector — and MySQL itself — do not recognise
-- :-prefixed placeholders, so we define MySQL session variables here and use
-- @startDate / @endDate in the WHERE clause instead.
--
-- Defaults below = every encounter on or before today (deliberately wide so a
-- weekly run never silently omits records). To bound the report to a specific
-- period, edit the two values, e.g. @startDate = '2025-10-01' for FY26 Q1.
-- ---------------------------------------------------------------------------
SET @startDate = '1900-01-01';
SET @endDate   = NOW();

SELECT
   global_property.property_value as DatimCode,
   MAX(IF(obs.concept_id=165567,obs.value_text, NULL))
   as `ANCNo`,
   pid2.identifier as PepfarID,
   pid3.identifier as HospID,
   person.gender as Sex,
   DATE_FORMAT(person.birthdate,'%d-%b-%Y') as DOB,
   TIMESTAMPDIFF(YEAR,person.birthdate,enc.encounter_datetime) as AgeAtVisit,
DATE_FORMAT(enc.encounter_datetime,'%d-%b-%Y')  as VisitDate,
visit.visit_id as VisitID,
MAX(IF(obs.concept_id=1427,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `DateOfLastMenstralPeriod`,
   MAX(IF(obs.concept_id=1438,obs.value_numeric, NULL))
   as `GestationAgeWeeks`,
   MAX(IF(obs.concept_id=5624,obs.value_numeric, NULL))
   as `Gravida`,
   MAX(IF(obs.concept_id=5596,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `ExpectedDeliveryDate`,
   MAX(IF(obs.concept_id=1053,obs.value_numeric, NULL)) 
   as `Parity`,
   MAX(IF(obs.concept_id=164894,obs.value_numeric, NULL)) 
   as `Losses`,
   MAX(IF(obs.concept_id=165280,cn1.name, NULL)) 
   as `WomanTestedForSyphilis`,
   MAX(IF(obs.concept_id= 164952,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `SyphilisTestDate`,
   MAX(IF(obs.concept_id= 299,cn1.name, NULL))
   as `SyphilisTestResult`,
   MAX(IF(obs.concept_id= 160733,cn1.name, NULL))
   as `TreatedForSyphilis`,
   MAX(IF(obs.concept_id= 164953,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `SyphilisTreatmentDate`,
   MAX(IF(obs.concept_id= 160312,cn1.name, NULL))
   as `ReferredSyphilisPositiveClient`,
   MAX(IF(obs.concept_id= 159430,cn1.name, NULL))
   as `HepatitisBTest`,
   MAX(IF(obs.concept_id= 161471,cn1.name, NULL))
   as `HepatitisCTest`,
   MAX(IF(obs.concept_id=165483,obs.value_text, NULL)) 
   as `FacilityReferredTo`,
   MAX(IF(obs.concept_id=166284,IF(obs.value_coded is not null,IF(obs.value_coded=1,"Yes","No"),""), NULL)) as KeyPopulation,
	MAX(IF(obs.concept_id=166369,cn1.name, NULL)) as KPType,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 166029,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `HTSRegisterDate`,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 166033,cn1.name, NULL))
   as `HIVRegisterSetting`,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 166030,cn1.name, NULL))
   as `PreviouslyKnownHIVPositive`,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 166031,DATE_FORMAT(obs.value_datetime,'%d-%b-%Y'), NULL))
   as `DatePreviouslyTestedHIVPositive`,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 159427,cn1.name, NULL))
   as `ResultOfHIVTest`,
   MAX(IF(enc.form_id=54 AND obs.concept_id= 166033,cn1.name, NULL))
   as `HIVReTesting`,
   MAX(IF(enc.form_id=40,DATE_FORMAT(enc.encounter_datetime,'%d-%b-%Y'), NULL)) as `PartnerTestingDate`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 164956,cn1.name, NULL))
   as `PartnerPretestCounseled`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 164956,cn1.name, NULL))
   as `PartnerAcceptsHIVTest`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 1436,cn1.name, NULL))
   as `PartnerHIVTestStatus`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 164959,cn1.name, NULL))
   as `PartnerPostTestCounseledReceivedResults`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 165561,cn1.name, NULL))
   as `PartnerHBVStatus`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 165562,cn1.name, NULL))
   as `PartnerHCVStatus`,
   MAX(IF(enc.form_id=40 AND obs.concept_id= 299,cn1.name, NULL))
   as `PartnerSyphilisStatus`,
   MAX(IF(enc.form_id=40 AND obs.concept_id=164960,cn1.name, NULL))
   as `PartnerReferralTo`,
   MAX(IF(enc.form_id=15,DATE_FORMAT(enc.encounter_datetime,'%d-%b-%Y'), NULL)) as `DeliveryDate`,
   MAX(IF(enc.form_id=15 AND obs.concept_id= 164851,cn1.name, NULL))
   as `TimeofHIVDiagnosesDeliveryRegister`,
   MAX(IF(enc.form_id=15 AND obs.concept_id= 1409,obs.value_numeric, NULL))
   as `GestationAgeWeeksAtDelivery`,
   MAX(IF(enc.form_id=15 AND obs.concept_id= 159430,cn1.name, NULL))
   as `HepatitisBAtDelivery`,
   MAX(IF(enc.form_id=15 AND obs.concept_id= 161471,cn1.name, NULL))
   as `HepatitisCAtDelivery`,
   MAX(IF(enc.form_id=15 AND obs.concept_id= 160119,cn1.name, NULL))
   as `CurrentlyTakingARV`,
   MAX(IF(enc.form_id=15 AND obs.concept_id=165563,cn1.name, NULL))
   as `ARTStartedInLabourAndDeliveryWard`,
   MAX(IF(enc.form_id=15 AND obs.concept_id=5630,cn1.name, NULL))
   as `ModeOfDelivery`,
   MAX(IF(enc.form_id=15 AND obs.concept_id=160085,cn1.name, NULL))
   as `MaternalOutcome`,
   MAX(IF(enc.form_id=15 AND obs.concept_id=165708,cn1.name, NULL))
   as `NumberOfChildren`,
   MAX(IF(enc.form_id=15 AND obs.concept_id=164968,cn1.name, NULL))
   as `ChildOutcomeAtDelivery`
    
  FROM
   obs
   LEFT JOIN patient ON(patient.patient_id=obs.person_id and obs.voided=0 and patient.voided=0)
   LEFT JOIN person ON(person.person_id=patient.patient_id and person.voided=0)
   LEFT join encounter enc on(enc.encounter_id=obs.encounter_id and enc.voided=0 and obs.voided=0 and enc.form_id IN (16,54,15,40,45))
   LEFT JOIN visit on (enc.visit_id=visit.visit_id and visit.voided=0)
   left join concept_name cn1 on(obs.value_coded=cn1.concept_id and cn1.locale='en' and cn1.locale_preferred=1)
   LEFT JOIN patient_identifier pid1 on(pid1.patient_id=patient.patient_id and patient.voided=0 and pid1.identifier_type=6 and pid1.voided=0)
   LEFT JOIN patient_identifier pid2 on(pid2.patient_id=patient.patient_id and patient.voided=0 and pid2.identifier_type=4 and pid2.voided=0)
   LEFT JOIN patient_identifier pid3 on(pid3.patient_id=patient.patient_id and patient.voided=0 and pid3.identifier_type=5 and pid3.voided=0)
   LEFT JOIN global_property on(global_property.property='facility_datim_code')
   WHERE patient.voided=0 
   and enc.voided=0 and 
   enc.encounter_datetime BETWEEN
   @startDate and @endDate
   GROUP BY patient.patient_id;
   
