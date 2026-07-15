# ADR-008: Domain model and storage boundaries

**Status:** Accepted  
**Date:** 15 July 2026

## Decision

### Storage

SQLite is the only MVP database. PostgreSQL is a future server-mode decision. Raw turns/events in SQLite are canonical; JSONL and Markdown are exports.

### Locations

`Character.current_location_id` is canonical. Location occupants are queried, not stored as a second mutable list.

### Items

An item has exactly one current placement: owner, location, container item or unknown. A database constraint enforces exclusivity.

### Goals

Goals are first-class rows with description, priority, status, secrecy, source and validity. They are not JSON arrays.

### Secrets and beliefs

Secret backstory is represented with facts, visibility and character beliefs whenever possible. Biography prose does not replace structured truth.

### Relationships

Relationships are assertion records with type, optional intensity, narrative description, reason, provenance, validity and visibility. Numeric dashboards may be derived later.

### Alternatives and undo

MVP uses `parent_turn_id` and turn status for regenerate/undo. Full named branch trees and replay are deferred.

## Consequences

Fewer synchronisation bugs, clearer provenance and simpler MVP storage. Some joins are accepted. PostgreSQL and relationship graphs remain future work.
