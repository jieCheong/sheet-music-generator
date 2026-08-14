import cors from "cors";
import express from "express";

const app = express();
const PORT = process.env.PORT ?? 3010;

app.use(cors({ origin: process.env.CLIENT_ORIGIN ?? "*" }));
app.use(express.json());

app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
