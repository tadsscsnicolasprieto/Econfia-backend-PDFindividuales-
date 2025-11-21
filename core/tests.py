
from django.test import TestCase
from core.models import Fuente, TipoFuente

class FuenteTestCase(TestCase):
	def setUp(self):
		self.tipo = TipoFuente.objects.create(nombre="TestTipo", peso=1, probabilidad=1)

	def test_creacion_fuente(self):
		fuente = Fuente.objects.create(nombre="FuenteTest", nombre_pila="FuenteTest", tipo=self.tipo)
		self.assertEqual(fuente.nombre, "FuenteTest")
		self.assertEqual(fuente.tipo, self.tipo)

	def test_fuente_unica_por_nombre(self):
		Fuente.objects.create(nombre="Unica", nombre_pila="Unica", tipo=self.tipo)
		with self.assertRaises(Exception):
			# Intentar crear otra fuente con el mismo nombre y tipo debería fallar si hay restricción de unicidad
			Fuente.objects.create(nombre="Unica", nombre_pila="Unica", tipo=self.tipo)
