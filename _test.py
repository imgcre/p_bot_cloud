from decimal import Decimal, ROUND_HALF_UP

unit = Decimal('0.1')

print(Decimal('-0.0001').quantize(unit, ROUND_HALF_UP).normalize())

