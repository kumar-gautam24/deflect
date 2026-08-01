-- One database per service. Each owns its own schema and migrations and never reads
-- another's tables; sharing one Postgres instance keeps local development to a single
-- container without weakening that boundary.
CREATE DATABASE deflect_retrieval;
CREATE DATABASE deflect_answer;
CREATE DATABASE deflect_evals;

CREATE DATABASE deflect_retrieval_test;
CREATE DATABASE deflect_answer_test;
CREATE DATABASE deflect_evals_test;
