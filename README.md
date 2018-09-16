# yadacoin
## Setup
  In your terminal
  Requires MongoDB:
  Ubuntu:
  `sudo apt-get install mongodb`

  `pip install -r requirements.txt`
  
  `python generate_config.py`
  
  copy that json object and paste it into a file named `config.json`

  place that file in a directory called `config`

IMPORTANT: You must run all three of the below processes for mining to work.

## Run the miner
`python p2p.py miner config.json`

## run the consensus script
`python p2p.py consensus config.json`

## run the server
`python p2p.py serve config.json`

The consensus and server must be running to run the miner.
