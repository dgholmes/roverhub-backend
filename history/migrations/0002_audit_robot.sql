-- The audit log gains the robot a row is about, so GET /audit can be filtered
-- by robot as well as by site, and indexes for both filters.
-- Rows written before this: commands and incidents are filled in from what they
-- already record; runs keep robot_id in `detail` only (reading JSON is not
-- portable between the two engines), so a robot filter starts with this level.

ALTER TABLE audit_log ADD COLUMN robot_id TEXT;
CREATE INDEX audit_by_robot ON audit_log (robot_id, at);
CREATE INDEX audit_by_site ON audit_log (site_id, at);
UPDATE audit_log SET robot_id = target WHERE action LIKE 'command.%';
UPDATE audit_log SET robot_id = (SELECT incidents.robot_id FROM incidents WHERE incidents.incident_id = audit_log.target) WHERE action LIKE 'incident.%';
