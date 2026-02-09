import csv
from typing import Optional


class DaliDatabase:
    def __init__(self, csv_fname: str) -> None:
        # here is example of csv file contents:
        # dali_product_id,brand_name,product_name,product_part_number,gtin,DALI Parts
        # 11125,Philips,Xi CR 40W .2-1.05A SDMP 230 C123 TR sXt,9290040147,8721103129109,"101,102,150,207,250,251,252,253"

        self._data_by_gtin = {}
        self._data_by_product_id = {}
        with open(csv_fname, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self._data_by_gtin[int(row["gtin"])] = row
                    self._data_by_product_id[int(row["dali_product_id"])] = row
                except ValueError:
                    continue

    def get_info_by_gtin(self, gtin: str) -> Optional[dict]:
        return self._data_by_gtin.get(gtin)

    def get_info_by_product_id(self, product_id: int) -> Optional[dict]:
        return self._data_by_product_id.get(product_id)
