const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

const ROOT = __dirname;

function configuredValue(name) {
  if (process.env[name]) return process.env[name].trim();
  const envFile = path.join(ROOT, ".env");
  if (!fs.existsSync(envFile)) return "";
  const line = fs.readFileSync(envFile, "utf8").split(/\r?\n/).find((entry) => entry.trim().startsWith(`${name}=`));
  return line ? line.slice(line.indexOf("=") + 1).trim().replace(/^(['"])(.*)\1$/, "$2") : "";
}

function resolveDatabasePath() {
  const configuredPath = configuredValue("DATABASE_PATH");
  const databaseUrl = configuredValue("DATABASE_URL");
  let dbPath = configuredPath;
  if (!dbPath && databaseUrl.startsWith("sqlite:///")) dbPath = databaseUrl.slice("sqlite:///".length);
  if (!dbPath && databaseUrl) throw new Error("Only SQLite DATABASE_URL values are supported.");
  if (!dbPath) dbPath = "attendance.db";
  return path.isAbsolute(dbPath) ? dbPath : path.resolve(ROOT, dbPath);
}

// Compatible with database.hash_password():
// pbkdf2_sha256$310000$<16-byte salt hex>$<32-byte digest hex>
function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  const digest = crypto.pbkdf2Sync(Buffer.from(password, "utf8"), salt, 310000, 32, "sha256");
  return `pbkdf2_sha256$310000$${salt.toString("hex")}$${digest.toString("hex")}`;
}

const dbPath = resolveDatabasePath();
const db = new DatabaseSync(dbPath);
const users = [
  {
    name: "AI Student",
    email: "ai_student@uob.edu.iq",
    password: "Student123",
    role: "student",
    class_id: "AI_Robotics",
    department: "هندسة الذكاء الاصطناعي والروبوتات",
    group_name: "B",
    display_role: "Student",
  },
  {
    name: "AI Representative",
    email: "ai_rep@uob.edu.iq",
    password: "Rep123",
    role: "rep",
    class_id: "AI_Robotics",
    department: "هندسة الذكاء الاصطناعي والروبوتات",
    group_name: "B",
    display_role: "Group B Representative",
  },
];

try {
  db.exec(`CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL,
    class_id TEXT,
    device_uuid TEXT,
    department TEXT,
    group_name TEXT,
    display_role TEXT,
    college TEXT,
    stage TEXT,
    is_first_login INTEGER NOT NULL DEFAULT 1
  )`);
  const columns = new Set(db.prepare("PRAGMA table_info(users)").all().map((column) => column.name));
  for (const [name, declaration] of [
    ["department", "TEXT"],
    ["group_name", "TEXT"],
    ["display_role", "TEXT"],
    ["college", "TEXT"],
    ["stage", "TEXT"],
    ["is_first_login", "INTEGER NOT NULL DEFAULT 1"],
  ]) {
    if (!columns.has(name)) db.exec(`ALTER TABLE users ADD COLUMN ${name} ${declaration}`);
  }

  db.exec("BEGIN IMMEDIATE");
  const findUser = db.prepare("SELECT id FROM users WHERE lower(email)=lower(?) LIMIT 1");
  const updateUser = db.prepare(`UPDATE users SET name=?,email=?,password=?,role=?,class_id=?,device_uuid=NULL,
    department=?,group_name=?,display_role=?,college='University of Baghdad',stage='Second Stage',is_first_login=0 WHERE id=?`);
  const insertUser = db.prepare(`INSERT INTO users
    (name,email,password,role,class_id,device_uuid,department,group_name,display_role,college,stage,is_first_login)
    VALUES (?,?,?,?,?,NULL,?,?,?,'University of Baghdad','Second Stage',0)`);
  for (const user of users) {
    const passwordHash = hashPassword(user.password);
    const existing = findUser.get(user.email);
    if (existing) {
      updateUser.run(user.name,user.email,passwordHash,user.role,user.class_id,user.department,user.group_name,user.display_role,existing.id);
    } else {
      insertUser.run(user.name,user.email,passwordHash,user.role,user.class_id,user.department,user.group_name,user.display_role);
    }
  }
  db.exec("COMMIT");
  console.log(`Seeded ${users.length} AI test accounts in ${dbPath}.`);
} catch (error) {
  try { db.exec("ROLLBACK"); } catch {}
  throw error;
} finally {
  db.close();
}
