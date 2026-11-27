import express from "express";
import Database from "better-sqlite3";

const app = express();
const db = new Database("app.db");

function sanitize(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, "");
}

function saveItem(itemName: string): void {
  db.prepare("INSERT INTO items (name) VALUES (?)").run(itemName);
}

app.post("/items", (req, res) => {
  const name = req.body.name;
  saveItem(name);
  res.json({ ok: true });
});

app.post("/safe", (req, res) => {
  const safeName = sanitize(req.body.name);
  saveItem(safeName);
  res.json({ ok: true });
});
