// Roles and users for the fact knowledge layer.
//
// Least privilege, three principals:
//   root  - cluster administration only, never used by the application
//   app   - read and write the four collections, and nothing else
//   ro    - read only, for inspecting findings without being able to change them
//
// The app role is a custom role rather than the built-in readWrite because
// readWrite carries dropCollection and dropDatabase. An ingest bug should be
// able to write a bad claim, not delete the corpus.
//
// Idempotent: re-running updates the roles and leaves existing users alone.

const DB = process.env.MONGO_DB || "factlayer";
const COLLECTIONS = ["pdfs", "evidence", "claims", "claim_groups"];

const db = globalThis.db.getSiblingDB(DB);

function upsertRole(name, privileges, roles) {
  const spec = { privileges, roles: roles || [] };
  try {
    db.createRole(Object.assign({ role: name }, spec));
    print(`created role ${name}`);
  } catch (e) {
    if (e.codeName !== "DuplicateKey" && !/already exists/i.test(e.message)) throw e;
    db.updateRole(name, spec);
    print(`updated role ${name}`);
  }
}

function upsertUser(name, pwd, roles) {
  if (!name || !pwd) throw new Error(`missing credentials for ${name || "(unnamed user)"}`);
  try {
    db.createUser({ user: name, pwd: pwd, roles: roles });
    print(`created user ${name}`);
  } catch (e) {
    if (e.codeName !== "Location51003" && !/already exists/i.test(e.message)) throw e;
    db.updateUser(name, { roles: roles });
    print(`user ${name} exists, roles updated`);
  }
}

upsertRole(
  "factsWriter",
  COLLECTIONS.map((c) => ({
    resource: { db: DB, collection: c },
    // No dropCollection, no dropDatabase, no createIndex teardown.
    actions: ["find", "insert", "update", "remove", "createIndex", "listIndexes", "listCollections"],
  }))
);

upsertRole(
  "factsReader",
  COLLECTIONS.map((c) => ({
    resource: { db: DB, collection: c },
    actions: ["find", "listIndexes", "listCollections"],
  }))
);

upsertUser(process.env.MONGO_APP_USER, process.env.MONGO_APP_PASSWORD, ["factsWriter"]);
upsertUser(process.env.MONGO_READONLY_USER, process.env.MONGO_READONLY_PASSWORD, ["factsReader"]);

print(`\nRBAC applied on ${DB}. Users: ${db.getUsers().users.map((u) => u.user).join(", ")}`);
