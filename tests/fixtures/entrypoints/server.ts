import express from 'express';
import axios from 'axios';

const app = express();

export interface Item {
  id: number;
}

app.get('/api/items', (req, res) => {
  res.json([]);
});

export async function fetchItems() {
  const r1 = await fetch('/api/remote');
  const r2 = await axios.get('/api/remote2');
}
