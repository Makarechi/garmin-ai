"""Track successful writes to insight-bearing projections within an ingest attempt."""


def execute_projection(session, statement):
    result = session.execute(statement.execution_options(preserve_rowcount=True))
    if result.rowcount > 0:
        session.info["projection_changed"] = True
    return result
