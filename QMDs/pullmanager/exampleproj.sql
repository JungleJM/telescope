USE PROJECTD33A929;

-- Drop and recreate destination table so schema is always aligned with this script
DROP TABLE IF EXISTS PROJECTD33A929.dbo.blkCrohnsPatients_SP;

CREATE TABLE PROJECTD33A929.dbo.blkCrohnsPatients_SP
(
    PatientDurableKey BIGINT NOT NULL,
    FirstRace VARCHAR(100) NULL,
    Sex VARCHAR(50) NULL,
    IndexAge INT NULL,
    ICDName VARCHAR(400) NULL,
    BirthDate DATE NULL,
    StateOrProvince VARCHAR(100) NULL,
    Country VARCHAR(100) NULL,
    ICDCode VARCHAR(400) NULL,
    IndexDate INT NULL,
    DiagnosisEventKey BIGINT NOT NULL,
    IndexEncounter BIGINT NOT NULL,
    SecondRace VARCHAR(100) NULL,
    ThirdRace VARCHAR(100) NULL,
    FourthRace VARCHAR(100) NULL,
    FifthRace VARCHAR(100) NULL,
    SviRacialEthnicMinorityStatusPctRankByZip2020_X FLOAT NULL,
    SviOverallPctIRankByZip2020_X FLOAT NULL,
    SviSocioeconomicPctIRankByZip2020_X FLOAT NULL
);

-- Local temp table to receive data from Cosmos via OPENQUERY
DROP TABLE IF EXISTS #Local_blkCrohnsPatients_SP;

SELECT
    PatientDurableKey, FirstRace, Sex, IndexAge, ICDName, BirthDate, StateOrProvince,
    Country, ICDCode, IndexDate, DiagnosisEventKey, IndexEncounter, SecondRace,
    ThirdRace, FourthRace, FifthRace,
    SviRacialEthnicMinorityStatusPctIRankByZip2020_X, SviOverallPctIRankByZip2020_X,
    SviSocioeconomicPctIRankByZip2020_X
INTO #Local_blkCrohnsPatients_SP
FROM OPENQUERY(
    [et4003vpdsq1032],
    '
        SELECT
            PatientDurableKey, FirstRace, Sex, IndexAge, ICDName, BirthDate,
            StateOrProvince, Country, ICDCode, IndexDate, DiagnosisEventKey,
            IndexEncounter, SecondRace, ThirdRace, FourthRace, FifthRace,
            SviRacialEthnicMinorityStatusPctIRankByZip2020_X,
            SviOverallPctIRankByZip2020_X,
            SviSocioeconomicPctIRankByZip2020_X
        FROM ##JVM_blkCrohnsPatients_SP
    '
);

-- Insert into destination table
INSERT INTO PROJECTD33A929.dbo.blkCrohnsPatients_SP (
    PatientDurableKey, FirstRace, Sex, IndexAge, ICDName, BirthDate, StateOrProvince,
    Country, ICDCode, IndexDate, DiagnosisEventKey, IndexEncounter, SecondRace,
    ThirdRace, FourthRace, FifthRace,
    SviRacialEthnicMinorityStatusPctIRankByZip2020_X, SviOverallPctIRankByZip2020_X,
    SviSocioeconomicPctIRankByZip2020_X
)
SELECT
    PatientDurableKey, FirstRace, Sex, IndexAge, ICDName, BirthDate, StateOrProvince,
    Country, ICDCode, IndexDate, DiagnosisEventKey, IndexEncounter, SecondRace,
    ThirdRace, FourthRace, FifthRace,
    SviRacialEthnicMinorityStatusPctIRankByZip2020_X, SviOverallPctIRankByZip2020_X,
    SviSocioeconomicPctIRankByZip2020_X
FROM #Local_blkCrohnsPatients_SP;

-- Cosmos-side row count for blkCrohnsPatients_SP (##JVM_blkCrohnsPatients_SP)
SELECT
    'blkCrohnsPatients_SP' AS CohortName,
    COUNT(*) AS CosmosRowCount
FROM OPENQUERY(
    [et4003vpdsq1032],
    '
        SELECT
            1 AS dummy
        FROM ##JVM_blkCrohnsPatients_SP
    '
);

-- Projects-side row count for blkCrohnsPatients_SP
SELECT
    'blkCrohnsPatients_SP' AS TableName,
    COUNT(*) AS blkCrohnsPatients_SPRowCount
FROM PROJECTD33A929.dbo.blkCrohnsPatients_SP;

-- Sample rows for blkCrohnsPatients_SP
SELECT TOP (1) *
FROM PROJECTD33A929.dbo.blkCrohnsPatients_SP;

DECLARE @EndTime_blkCrohnsPatients_SP DATETIME2 = SYSDATETIME();
PRINT 'Projects cohort end: blkCrohnsPatients_SP (blkCrohnsPatients_SP) at '
    + CONVERT(VARCHAR(30), @EndTime_blkCrohnsPatients_SP, 126);

DECLARE @ElapsedSeconds_blkCrohnsPatients_SP INT =
    DATEDIFF(SECOND, @StartTime_blkCrohnsPatients_SP, @EndTime_blkCrohnsPatients_SP);
DECLARE @ElapsedMinutes_blkCrohnsPatients_SP INT = @ElapsedSeconds_blkCrohnsPatients_SP / 60;
DECLARE @RemainingSeconds_blkCrohnsPatients_SP INT = @ElapsedSeconds_blkCrohnsPatients_SP % 60;

PRINT 'Projects cohort duration (seconds): ' +
    CONVERT(VARCHAR(30), @ElapsedSeconds_blkCrohnsPatients_SP);

PRINT 'Projects cohort duration (minutes:seconds): ' +
    CONVERT(VARCHAR(30), @ElapsedMinutes_blkCrohnsPatients_SP) + 'm ' +
    CONVERT(VARCHAR(30), @RemainingSeconds_blkCrohnsPatients_SP) + 's';