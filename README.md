# check_vm_files.py

**Icinga / NetEye monitoring plugin** per il conteggio dei file VMDK e snapshot presenti nella cartella datastore di una VM specifica, tramite vCenter API (pyVmomi).

---

## Panoramica

Lo script conta i file presenti nella directory di una VM su datastore, utile per:

- **Rilevare accumulo di snapshot** non consolidati (file `*-0000*.vmdk`, `*.vmsn`, `*.vmsd`, `*.delta.vmdk`)
- **Monitorare il numero di dischi VMDK** per individuare crescita anomala o file orfani
- **Prevenire problemi di spazio** e prestazioni causati da catene di snapshot troppo lunghe

| Modalita' | Cosa conta | Use case |
|---|---|---|
| **allfiles** (default) | Tutti i file `*.vmdk` nella cartella VM | Conteggio dischi totali |
| **snaponly** (`--snaponly`) | Solo file snapshot (`*-0000*.vmdk`, `*.vmsn`, `*.vmsd`, `*.delta.vmdk`) | Rilevamento snapshot non consolidati |

---

## Logica degli exit code

| Exit Code | Stato | Condizione |
|---|---|---|
| `0` | **OK** | Numero file sotto le soglie |
| `1` | **WARNING** | `files >= warning threshold` (solo se `-w` > 0) |
| `2` | **CRITICAL** | `files > critical threshold` |
| `3` | **UNKNOWN** | VM non trovata, datastore non accessibile, errore di browse |

---

## Requisiti

- **Python 3.6+**
- **pyVmomi** (VMware vSphere API Python Bindings)

```bash
pip3 install pyvmomi
```

### Permessi richiesti

| Componente | Permesso |
|---|---|
| vCenter | Utente con permessi di lettura sull'inventario e browse datastore (`Datastore.Browse`, `System.View`, `System.Read`) |

---

## Porte di rete richieste

| Sorgente | Destinazione | Porta | Protocollo | Descrizione |
|---|---|---|---|---|
| Monitoring server | vCenter Server | **443/tcp** | HTTPS | vSphere API (pyVmomi / SOAP) |

> **Nota:** Lo script comunica esclusivamente con il vCenter Server. Il browse del datastore avviene tramite le API vSphere, non con connessione diretta agli host ESXi o ai datastore.

---

## Installazione

```bash
# Clona il repository
git clone https://github.com/GiulioSavini/check-vm-files.git
cd check-vm-files

# Installa le dipendenze
pip3 install pyvmomi

# Rendi eseguibile
chmod +x check_vm_files.py

# (Opzionale) Copia nella directory dei plugin Icinga/NetEye
cp check_vm_files.py /usr/lib/nagios/plugins/
```

---

## Sintassi e parametri

```
check_vm_files.py -H <vcenter> -u <user> -p <password> -v <vm_name> [opzioni]
```

### Parametri obbligatori

| Parametro | Descrizione |
|---|---|
| `-H`, `--host` | Indirizzo IP o hostname del vCenter Server |
| `-u`, `--user` | Username per l'autenticazione al vCenter |
| `-p`, `--password` | Password per l'autenticazione al vCenter |
| `-v`, `--vm` | Nome esatto della VM da controllare |

### Parametri opzionali

| Parametro | Default | Descrizione |
|---|---|---|
| `-w`, `--warning` | `0` (disabilitato) | Soglia WARNING: WARN se `files >= N`. Valore `0` disabilita il warning. |
| `-c`, `--critical` | `40` | Soglia CRITICAL: CRIT se `files > N` |
| `--recursive` | `false` | Cerca ricorsivamente nelle sottocartelle del datastore |
| `--snaponly` | `false` | Conta solo file relativi a snapshot (`*-0000*.vmdk`, `*.vmsn`, `*.vmsd`, `*.delta.vmdk`) |

---

## Esempi di utilizzo

### Check base - tutti i VMDK

```bash
./check_vm_files.py -H <VCENTER_HOST> -u <USERNAME> -p '<PASSWORD>' -v myserver01
```

Output:
```
OK - files=4 vm='myserver01' ds='datastore1' path='[datastore1] myserver01' mode=allfiles scope=folder | files=4;0;40;0;
```

### Check solo snapshot con soglie personalizzate

```bash
./check_vm_files.py \
  -H <VCENTER_HOST> \
  -u <USERNAME> \
  -p '<PASSWORD>' \
  -v myserver01 \
  --snaponly \
  -w 5 \
  -c 20
```

Output (WARNING - 7 file snapshot trovati):
```
WARNING - files=7 vm='myserver01' ds='datastore1' path='[datastore1] myserver01' mode=snaponly scope=folder | files=7;5;20;0;
```

### Check ricorsivo nelle sottocartelle

```bash
./check_vm_files.py \
  -H <VCENTER_HOST> \
  -u <USERNAME> \
  -p '<PASSWORD>' \
  -v myserver01 \
  --recursive
```

### Esempio output CRITICAL

```
CRITICAL - files=53 vm='myserver01' ds='datastore1' path='[datastore1] myserver01' mode=allfiles scope=folder | files=53;0;40;0;
```

### Esempio output UNKNOWN - VM non trovata

```
UNKNOWN - VM 'nonexistent-vm' not found
```

---

## Performance Data (perfdata)

Lo script emette perfdata compatibili con Icinga/Nagios dopo il pipe `|`:

| Metrica | Formato | Descrizione |
|---|---|---|
| `files` | `files=<count>;<warn>;<crit>;0;` | Numero di file trovati, con soglie WARNING e CRITICAL |

La perfdata segue il formato standard Nagios: `label=value;warn;crit;min;max`

---

## Dettagli tecnici

### Architettura dello script

```
check_vm_files.py
├── get_args()            # Parsing argomenti CLI
├── find_vm_by_name()     # Cerca VM per nome esatto tramite ContainerView
├── parse_vm_path()       # Parsa "[datastore] folder/vm.vmx" -> (ds_name, folder)
├── wait_task()           # Attende completamento task asincrono vSphere
└── main()
    ├── Connessione al vCenter (SmartConnect)
    ├── Ricerca VM per nome
    ├── Parsing vmPathName per identificare datastore e cartella
    ├── Risoluzione oggetto Datastore
    ├── Browse datastore (SearchDatastore_Task o SearchDatastoreSubFolders_Task)
    ├── Conteggio file
    └── Valutazione soglie e output
```

### Come funziona il rilevamento della cartella VM

1. Lo script legge `vm.config.files.vmPathName` (es: `[datastore1] myserver01/myserver01.vmx`)
2. Estrae il **nome del datastore** e il **percorso della cartella** dalla stringa
3. Risolve l'oggetto `vim.Datastore` corrispondente (prima tra i datastore della VM, poi fallback globale)
4. Usa il `DatastoreBrowser` per cercare i file nella cartella

### Pattern di ricerca

| Modalita' | Pattern | File matchati |
|---|---|---|
| **allfiles** (default) | `*.vmdk` | Tutti i dischi virtuali (base, flat, snapshot delta, sesparse) |
| **snaponly** | `*-0000*.vmdk`, `*.vmsn`, `*.vmsd`, `*.delta.vmdk` | Solo file generati da snapshot |

### Dettaglio file snapshot

| Estensione / Pattern | Descrizione |
|---|---|
| `*-0000*.vmdk` | Dischi delta di snapshot (es: `vm-000001.vmdk`, `vm-000002.vmdk`) |
| `*.delta.vmdk` | Dischi delta in formato alternativo |
| `*.vmsn` | Snapshot memory state (stato della memoria al momento dello snapshot) |
| `*.vmsd` | Snapshot metadata (dizionario degli snapshot della VM) |

### Ricerca ricorsiva vs cartella singola

| Opzione | Metodo API | Comportamento |
|---|---|---|
| default | `SearchDatastore_Task` | Cerca solo nella cartella principale della VM |
| `--recursive` | `SearchDatastoreSubFolders_Task` | Cerca anche in tutte le sottocartelle |

La ricerca ricorsiva e' utile quando i file della VM sono distribuiti su piu' sottocartelle dello stesso datastore.

### Gestione task asincroni

Il browse del datastore e' un'operazione asincrona in vSphere. Lo script usa `wait_task()` che effettua polling ogni 200ms sullo stato del task fino al completamento o errore.

---

## Configurazione Icinga / NetEye

### CheckCommand definition

```
object CheckCommand "check_vm_files" {
  command = [ PluginDir + "/check_vm_files.py" ]
  arguments = {
    "-H" = "$vm_files_host$"
    "-u" = "$vm_files_user$"
    "-p" = "$vm_files_password$"
    "-v" = "$vm_files_vm$"
    "-w" = "$vm_files_warning$"
    "-c" = "$vm_files_critical$"
    "--recursive" = {
      set_if = "$vm_files_recursive$"
    }
    "--snaponly" = {
      set_if = "$vm_files_snaponly$"
    }
  }
}
```

### Service definition - Check snapshot per singola VM

```
apply Service "vm-files-snapshot" {
  check_command = "check_vm_files"
  vars.vm_files_host = "<VCENTER_HOST>"
  vars.vm_files_user = "<USERNAME>"
  vars.vm_files_password = "<PASSWORD>"
  vars.vm_files_vm = host.name
  vars.vm_files_snaponly = true
  vars.vm_files_warning = 5
  vars.vm_files_critical = 20
  check_interval = 30m
  retry_interval = 5m
  assign where host.vars.role == "vm"
}
```

### Service definition - Check VMDK totali

```
apply Service "vm-files-total" {
  check_command = "check_vm_files"
  vars.vm_files_host = "<VCENTER_HOST>"
  vars.vm_files_user = "<USERNAME>"
  vars.vm_files_password = "<PASSWORD>"
  vars.vm_files_vm = host.name
  vars.vm_files_critical = 40
  check_interval = 1h
  retry_interval = 10m
  assign where host.vars.role == "vm"
}
```

---

## Perche' monitorare i file VM

### Snapshot non consolidati

Gli snapshot VMware creano file delta (`*-0000*.vmdk`) che crescono nel tempo. Se non vengono consolidati:

- **Occupano spazio disco** in modo progressivo e imprevedibile
- **Degradano le prestazioni I/O** della VM (ogni operazione di lettura deve attraversare la catena di snapshot)
- **Possono causare downtime** se il datastore si riempie
- **Complicano i backup** e le operazioni di manutenzione

### Soglie raccomandate

| Scenario | Warning | Critical | Modalita' |
|---|---|---|---|
| Monitoraggio snapshot | `-w 3` | `-c 10` | `--snaponly` |
| Monitoraggio VMDK totali | `-w 20` | `-c 40` | default |
| VM con molti dischi (DB, file server) | `-w 30` | `-c 60` | default |

---

## Licenza

MIT License
