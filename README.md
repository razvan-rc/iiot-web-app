
---

# 🏭 Distributed Cloud-Native IIoT Monitoring Platform

## 📌 Overview

This project presents a scalable, **4-tier Cloud-Native architecture** deployed on AWS, designed for the real-time monitoring and predictive maintenance of industrial equipment.

It acts as a Digital Twin for a Festo modular production system, acquiring telemetry, calculating the **Remaining Useful Life (RUL)** via linear regression, and displaying the overall equipment effectiveness (OEE) through an asynchronous SCADA-like Human-Machine Interface (HMI).

## 🏗️ Architectural Topology

The environment completely decouples the data ingestion from the presentation layer to ensure High Availability and zero database locks during peak loads.

1. **Edge Node / Simulator (Ingestion):** A Python-based `systemd` service acting as an industrial IoT Gateway. It simulates physical wear/faults and pushes JSON payloads directly to the Cloud.
2. **DB Master (Write-Only):** The Single Source of Truth. Handles all raw telemetry inserts and acts as a persistent message queue for maintenance commands.
3. **DB Slave (Read-Replica):** Asynchronously replicates data from the Master. Exclusively handles complex SQL queries and serves the Web API.
4. **Web Server (Backend & Frontend):** Hosts the Flask REST API (via Gunicorn/Nginx) and the HMI Dashboard. Uses async polling (30s intervals) to fetch data with minimal network overhead.

## ✨ Key Features

* **Infrastructure as Code (IaC):** The entire AWS server configuration (Databases, Nginx, Linux services) is fully automated, idempotent, and reproducible using **Ansible**.
* **Continuous Integration & Deployment (CI/CD):** Automated delivery pipeline via **GitHub Actions**. Every push to the `main` branch tests and deploys the Flask application seamlessly.
* **Database Read/Write Splitting:** Implemented MariaDB asynchronous replication to separate the heavy Edge ingestion traffic from the Web API read requests, utilizing *Eventual Consistency*.
* **Defense in Depth (Security):**
* End-to-End Encryption via Let's Encrypt (Certbot) / HTTPS.
* AWS Security Groups enforcing network isolation.
* *Least-Privilege Principle* applied to Database users (Write access on Master, Read-Only on Slave).


* **Predictive Maintenance:** Dynamic RUL calculation based on synthetic sensor degradation, triggering visual CSS alerts (`warn`, `critical`) on the HMI.

## 🚀 Deployment & Operations

Because the infrastructure relies heavily on declarative IaC, the manual setup is minimal.

1. **Provision AWS Resources:** Spin up 4 EC2 instances (Ubuntu) and configure Security Groups.
2. **Configure Inventory:** Update the Ansible `hosts.ini` with the target private/public IPs.
3. Run the Playbook:bash
ansible-playbook site.yml
```
*Ansible will automatically build the databases, establish Master-Slave replication, create users, install the Web Server, clone the repository, and issue SSL/TLS certificates.*


```



## 📊 HMI Dashboard

The frontend is a lightweight, asynchronous interface that queries the Flask API. It relies on standard web technologies (HTML, CSS, JS) and Fetch API to update live indicators and degradation bars dynamically without reloading the page, making it highly efficient for prolonged industrial monitoring.
